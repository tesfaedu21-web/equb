from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session, selectinload
from sqlalchemy import func
from pydantic import BaseModel, model_validator
from typing import Optional, List, Literal
from datetime import datetime, date as _date, timezone, timedelta
from collections import defaultdict
from database import get_db, Payment, PaymentBatch, Member, MemberSpot, Week, Cycle, Settings, log_action, _eat_now, _eat_today
from routers.notifications import send_payment_confirmed
from routers.deps import _require_admin, _get_current_user


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _eth_year(dt) -> int:
    """Return the Ethiopian calendar year for a given Gregorian date.
    Enkutatash (Meskerem 1) falls on Sep 12 in Gregorian years where dt.year % 4 == 3
    (i.e. 2019, 2023, 2027 …) — the year after an Ethiopian leap year.
    """
    new_year_day = 12 if dt.year % 4 == 3 else 11
    if (dt.month, dt.day) >= (9, new_year_day):
        return dt.year - 7
    return dt.year - 8


_ETH_MONTHS = ['መስከረም','ጥቅምት','ህዳር','ታህሳስ','ጥር','የካቲት','መጋቢት','ሚያዝያ','ግንቦት','ሰኔ','ሐምሌ','ነሐሴ','ጳጉሜ']

def _greg_to_eth(dt) -> str:
    """Convert a Gregorian datetime/date to a formatted Ethiopian calendar string."""
    y, mo, da = dt.year, dt.month, dt.day
    a   = (14 - mo) // 12
    yy  = y + 4800 - a
    mm  = mo + 12 * a - 3
    jdn = da + (153 * mm + 2) // 5 + 365 * yy + yy // 4 - yy // 100 + yy // 400 - 32045
    diff = jdn - 1724221
    k, n = divmod(diff, 1461)
    s   = 4 * k + 1
    l0  = 366 if s % 4 == 3 else 365
    l1  = 366 if (s + 1) % 4 == 3 else 365
    l2  = 366 if (s + 2) % 4 == 3 else 365
    if n < l0:
        ey, dn = s, n
    elif n < l0 + l1:
        ey, dn = s + 1, n - l0
    elif n < l0 + l1 + l2:
        ey, dn = s + 2, n - l0 - l1
    else:
        ey, dn = s + 3, n - l0 - l1 - l2
    return f"{dn % 30 + 1} {_ETH_MONTHS[min(dn // 30, 12)]} {ey}"


def _dual_date(dt) -> str:
    """Return an HTML snippet showing both Ethiopian and Gregorian dates stacked."""
    if not dt:
        return "—"
    eth  = _greg_to_eth(dt)
    greg = dt.strftime("%d %b %Y")
    return (f'<span style="text-align:right">'
            f'<div>{eth}</div>'
            f'<div style="font-size:11px;color:#9ca3af;margin-top:1px">{greg}</div>'
            f'</span>')


def _receipt_no(payment) -> str:
    """Generate a receipt number: RCP-{eth_year}-W{week}-{id}
    Batch payments use batch ID + highest week in the batch (= the week when
    payment was recorded). Single payments use payment ID + their own week.
    All payments in the same batch return the same receipt number.
    """
    if payment.batch_id and payment.batch:
        b   = payment.batch
        dt  = b.payment_date or payment.paid_date or _utcnow()
        max_week = max((bp.week.week_number for bp in b.payments if bp.week), default=0)
        return f"RCP-{_eth_year(dt):04d}-W{max_week:02d}-{b.id:05d}"
    dt  = payment.paid_date if payment.paid_date else _utcnow()
    wk  = payment.week.week_number if payment.week else 0
    return f"RCP-{_eth_year(dt):04d}-W{wk:02d}-{payment.id:05d}"


router = APIRouter()

METHODS = {"cash", "bank_transfer", "cheque"}


def _calc_penalty(payment: Payment, paid_date: datetime, db: Session) -> float:
    """Return auto-calculated penalty ETB, or 0 if not applicable."""
    try:
        gs = db.query(Settings).first()
        if not gs or not gs.penalty_rate:
            return 0.0
        week = payment.week
        if not week or not week.draw_date:
            return 0.0
        grace = gs.penalty_grace_days or 0
        due = week.draw_date.date() + timedelta(days=grace)
        if paid_date.date() > due:
            return round(payment.amount * gs.penalty_rate / 100, 2)
    except Exception:
        pass
    return 0.0


class PaymentUpdate(BaseModel):
    status: Literal["pending", "paid", "late", "missed"]
    paid_date: Optional[str] = None
    payment_method: Optional[str] = None
    reference: Optional[str] = None
    notes: Optional[str] = None
    penalty_amount: Optional[float] = None  # late-payment penalty in ETB


class BatchPaymentRecord(BaseModel):
    """Record one physical payment covering multiple past weeks."""
    member_id: int
    week_ids: List[int]
    payment_date: Optional[str] = None
    payment_method: str = "cash"
    reference: Optional[str] = None
    notes: Optional[str] = None
    penalty_amount: Optional[float] = None

    @model_validator(mode="after")
    def week_ids_not_empty(self):
        if not self.week_ids:
            raise ValueError("week_ids must not be empty")
        return self


class BulkPayment(BaseModel):
    week_id: int
    member_ids: List[int]
    status: Literal["pending", "paid", "late", "missed"]
    payment_method: Optional[str] = "cash"
    paid_date: Optional[str] = None


def payment_to_dict(p: Payment, cycle_id: Optional[int] = None) -> dict:
    spot_numbers = []
    if p.member:
        if cycle_id is not None:
            spot_numbers = [sa.spot.number for sa in p.member.spot_assignments
                            if sa.is_active and sa.cycle_id == cycle_id]
        else:
            spot_numbers = [sa.spot.number for sa in p.member.spot_assignments if sa.is_active]
    return {
        "id": p.id,
        "member_id": p.member_id,
        "member_name": p.member.name if p.member else None,
        "spot_numbers": spot_numbers,
        "week_id": p.week_id,
        "week_number": p.week.week_number if p.week else None,
        "draw_date": p.week.draw_date.isoformat() if p.week else None,
        "batch_id": p.batch_id,
        "amount": p.amount,
        "paid_date": p.paid_date.isoformat() if p.paid_date else None,
        "payment_method": p.payment_method,
        "reference": p.reference,
        "status": p.status,
        "notes": p.notes,
        "penalty_amount": p.penalty_amount or 0,
        "collected_by_id": p.collected_by_id,
        "collected_by_name": p.collected_by.full_name if p.collected_by else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        "receipt_no": _receipt_no(p) if p.status == "paid" else None,
    }


def batch_to_dict(b: PaymentBatch) -> dict:
    return {
        "id": b.id,
        "member_id": b.member_id,
        "member_name": b.member.name if b.member else None,
        "payment_date": b.payment_date.isoformat(),
        "weeks_paid": b.weeks_paid,
        "total_amount": b.total_amount,
        "payment_method": b.payment_method,
        "reference": b.reference,
        "notes": b.notes,
        "week_numbers": sorted([p.week.week_number for p in b.payments if p.week]),
        "collected_by_name": b.collected_by.full_name if b.collected_by else None,
    }


@router.get("/week/{week_id}")
def payments_for_week(week_id: int, db: Session = Depends(get_db)):
    w = db.query(Week).filter(Week.id == week_id).first()
    if not w:
        raise HTTPException(status_code=404, detail="Week not found")

    cycle_id = w.cycle_id

    # ── Members in THIS cycle only ────────────────────────────────────────────
    cycle_spots = db.query(MemberSpot).filter(
        MemberSpot.cycle_id == cycle_id,
        MemberSpot.is_active == True,
    ).all()

    # Sum weekly contribution per member across all their spots in this cycle
    member_amounts: dict = defaultdict(float)
    for ms in cycle_spots:
        member_amounts[ms.member_id] += ms.weekly_contribution
    cycle_member_ids = set(member_amounts.keys())

    # ── Clean up payments for members NOT in this cycle (stale global records) ─
    if cycle_member_ids:
        bad = db.query(Payment).filter(
            Payment.week_id == week_id,
            ~Payment.member_id.in_(cycle_member_ids),
            Payment.status == "pending",
        ).all()
    else:
        bad = db.query(Payment).filter(
            Payment.week_id == week_id, Payment.status == "pending"
        ).all()
    for p in bad:
        db.delete(p)
    if bad:
        db.flush()

    # ── Create missing payment records for cycle members ──────────────────────
    existing_ids = {p.member_id for p in db.query(Payment).filter(Payment.week_id == week_id).all()}
    for member_id, amount in member_amounts.items():
        if member_id not in existing_ids and amount > 0:
            db.add(Payment(member_id=member_id, week_id=week_id, amount=amount))

    db.commit()
    db.expire(w)

    payments = (db.query(Payment).filter(Payment.week_id == week_id)
                .join(Member)
                .options(
                    selectinload(Payment.batch).selectinload(PaymentBatch.payments).selectinload(Payment.week),
                    selectinload(Payment.collected_by),
                    selectinload(Payment.member),
                )
                .order_by(Member.name).all())
    return [payment_to_dict(p, cycle_id) for p in payments]


@router.get("/member/{member_id}")
def payments_for_member(member_id: int, cycle_id: Optional[int] = None,
                        db: Session = Depends(get_db)):
    if not cycle_id:
        active = db.query(Cycle).filter(Cycle.status == "active").order_by(Cycle.id.desc()).first()
        cycle_id = active.id if active else None
    q = (db.query(Payment).filter(Payment.member_id == member_id)
         .join(Week)
         .options(
             selectinload(Payment.batch).selectinload(PaymentBatch.payments).selectinload(Payment.week),
             selectinload(Payment.collected_by),
             selectinload(Payment.week),
         ))
    if cycle_id:
        q = q.filter(Week.cycle_id == cycle_id)
    payments = q.order_by(Week.week_number).all()
    return [payment_to_dict(p, cycle_id) for p in payments]


@router.get("/member/{member_id}/outstanding")
def outstanding_weeks(member_id: int, include_week_id: Optional[int] = None,
                      db: Session = Depends(get_db)):
    """
    Return all unpaid weeks for a member.
    Auto-generates missing payment records for every week in the active cycle
    so that per-member missed weeks are always tracked regardless of whether
    that week's payment page was ever opened.
    include_week_id: always include this week even if draw_date is in the future
    (used when the cashier clicks a specific week row to record payment for it).
    """
    member = db.query(Member).filter(Member.id == member_id).first()
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")

    # Use active-cycle-specific contribution amount
    active_cycle = db.query(Cycle).filter(Cycle.status == "active").order_by(Cycle.id.desc()).first()
    cycle_id = active_cycle.id if active_cycle else None
    amount = sum(
        sa.weekly_contribution for sa in member.spot_assignments
        if sa.is_active and (cycle_id is None or sa.cycle_id == cycle_id)
    ) or 0

    if amount > 0:
        # Get all weeks in the active cycle
        all_weeks = (db.query(Week).join(Cycle)
                     .filter(Cycle.status == "active")
                     .order_by(Week.week_number).all())
        existing_week_ids = {
            p.week_id for p in
            db.query(Payment.week_id).filter(Payment.member_id == member_id).all()
        }
        new_records = [
            Payment(member_id=member_id, week_id=w.id, amount=amount)
            for w in all_weeks if w.id not in existing_week_ids
        ]
        if new_records:
            db.bulk_save_objects(new_records)
            db.commit()

    now = _eat_now()
    q = (db.query(Payment)
         .filter(Payment.member_id == member_id,
                 Payment.status.in_(["pending", "late", "missed"]))
         .join(Week)
         .options(
             selectinload(Payment.batch).selectinload(PaymentBatch.payments).selectinload(Payment.week),
             selectinload(Payment.week),
         ))
    # Always scope to the active cycle so old-cycle debt never bleeds in
    if cycle_id:
        q = q.filter(Week.cycle_id == cycle_id)
    # Only show weeks whose draw_date has arrived — no future week payments allowed,
    # except the explicitly requested week (cashier opened that week's row directly).
    if include_week_id:
        q = q.filter((Week.draw_date <= now) | (Week.id == include_week_id))
    else:
        q = q.filter(Week.draw_date <= now)
    payments = q.order_by(Week.week_number).all()
    return {
        "member_id": member_id,
        "member_name": member.name,
        "outstanding": [payment_to_dict(p) for p in payments],
        "total_owed": sum(p.amount for p in payments),
    }


@router.post("/batch-record")
def record_batch_payment(data: BatchPaymentRecord, request: Request, db: Session = Depends(get_db)):
    """
    Record one physical payment event for a member covering multiple weeks.
    Partial payment is allowed — only the weeks in week_ids are marked paid.
    """
    if not data.week_ids:
        raise HTTPException(status_code=400, detail="No weeks selected")

    # Reject future payment dates
    pay_date = datetime.fromisoformat(data.payment_date) if data.payment_date else _utcnow()
    if pay_date.date() > _eat_today():
        raise HTTPException(status_code=400, detail="Payment date cannot be in the future")

    member = db.query(Member).filter(Member.id == data.member_id).first()
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")
    if member.status == "left":
        raise HTTPException(status_code=400, detail="Cannot record payment for a member who has left")

    payments = (db.query(Payment)
                .filter(Payment.member_id == data.member_id,
                        Payment.week_id.in_(data.week_ids)).all())
    if not payments:
        raise HTTPException(status_code=404, detail="No matching payment records found")

    # Validate amounts
    for p in payments:
        if (p.amount or 0) <= 0:
            raise HTTPException(status_code=400, detail=f"Payment amount must be greater than zero (week {p.week_id})")

    # Idempotency guard: reject if any week is already paid to prevent double-charge
    already_paid = [p for p in payments if p.status == "paid"]
    if already_paid:
        paid_weeks = sorted([p.week.week_number for p in already_paid if p.week])
        raise HTTPException(
            status_code=409,
            detail=f"Week(s) {paid_weeks} are already recorded as paid. Check for duplicate submission.",
        )

    total = sum(p.amount for p in payments)
    cashier_id = getattr(request.state, "user_id", None)

    batch = PaymentBatch(
        member_id=data.member_id,
        payment_date=pay_date,
        weeks_paid=len(payments),
        total_amount=total,
        payment_method=data.payment_method,
        reference=data.reference,
        notes=data.notes,
        collected_by_id=cashier_id,
    )
    db.add(batch)
    db.flush()

    for p in payments:
        p.status = "paid"
        p.paid_date = pay_date
        p.payment_method = data.payment_method
        p.reference = data.reference
        p.batch_id = batch.id
        p.collected_by_id = cashier_id
        if data.penalty_amount is None:
            p.penalty_amount = _calc_penalty(p, pay_date, db)

    db.commit()
    db.refresh(batch)
    user = _get_current_user(request, db)
    log_action(db, user=user, action="create", table="payment_batches",
               record_id=batch.id,
               description=f"Recorded {len(payments)} payment(s) for {member.name} — {total:,.0f} ETB via {data.payment_method}",
               new={"member_id": data.member_id, "weeks": data.week_ids,
                    "total": float(total), "method": data.payment_method})
    db.commit()
    sms_status = send_payment_confirmed(payments[0], db) if payments else "skipped"
    result = batch_to_dict(batch)
    result["sms_status"] = sms_status
    return result


@router.put("/{payment_id}")
def update_payment(payment_id: int, data: PaymentUpdate, request: Request, db: Session = Depends(get_db)):
    p = db.query(Payment).filter(Payment.id == payment_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Payment not found")

    caller_role = getattr(request.state, "user_role", "cashier")

    # Only admin/superadmin can un-pay (reset paid → pending/late/missed)
    if p.status == "paid" and data.status != "paid":
        if caller_role not in ("admin", "superadmin"):
            raise HTTPException(
                status_code=403,
                detail="Only an admin can reverse a recorded payment. Contact your administrator.",
            )

    # Reject future paid_date
    if data.paid_date:
        pd = datetime.fromisoformat(data.paid_date)
        if pd.date() > _eat_today():
            raise HTTPException(status_code=400, detail="Payment date cannot be in the future")

    old_status = p.status
    was_unpaid = p.status != "paid"
    p.status = data.status
    if data.payment_method:
        p.payment_method = data.payment_method
    if data.reference is not None:
        p.reference = data.reference
    if data.paid_date:
        p.paid_date = datetime.fromisoformat(data.paid_date)
    elif data.status == "paid" and not p.paid_date:
        p.paid_date = _utcnow()
    if data.notes is not None:
        p.notes = data.notes
    if data.penalty_amount is not None:
        p.penalty_amount = data.penalty_amount
    elif data.status == "paid" and was_unpaid:
        p.penalty_amount = _calc_penalty(p, p.paid_date or _utcnow(), db)
    if data.status == "paid" and was_unpaid:
        p.collected_by_id = getattr(request.state, "user_id", None)

    user = _get_current_user(request, db)
    log_action(db, user=user, action="update", table="payments",
               record_id=p.id,
               description=f"Payment status changed: {old_status} → {data.status}",
               old={"status": old_status},
               new={"status": data.status, "method": data.payment_method})
    db.commit()
    sms_status = "skipped"
    if data.status == "paid" and was_unpaid:
        sms_status = send_payment_confirmed(p, db)
    result = payment_to_dict(p)
    result["sms_status"] = sms_status
    return result


@router.post("/bulk")
def bulk_update(data: BulkPayment, request: Request, db: Session = Depends(get_db)):
    # Bulk status changes require admin role
    _require_admin(request)
    paid_date = datetime.fromisoformat(data.paid_date) if data.paid_date else _utcnow()
    if paid_date.date() > _eat_today():
        raise HTTPException(status_code=400, detail="Payment date cannot be in the future")
    cashier_id = getattr(request.state, "user_id", None)
    updated = 0
    for mid in data.member_ids:
        p = db.query(Payment).filter(
            Payment.week_id == data.week_id, Payment.member_id == mid).first()
        if not p:
            m = db.query(Member).filter(Member.id == mid).first()
            w_obj = db.query(Week).filter(Week.id == data.week_id).first()
            if m and w_obj:
                cid = w_obj.cycle_id
                amount = sum(sa.weekly_contribution for sa in m.spot_assignments
                             if sa.is_active and sa.cycle_id == cid) or 0
                p = Payment(member_id=mid, week_id=data.week_id, amount=amount)
                db.add(p)
                db.flush()
        if p:
            p.status = data.status
            if data.payment_method:
                p.payment_method = data.payment_method
            if data.status == "paid":
                p.paid_date = paid_date
                p.collected_by_id = cashier_id
                p.penalty_amount = _calc_penalty(p, paid_date, db)
                try:
                    send_payment_confirmed(p, db)
                except Exception:
                    pass
            updated += 1
    user = _get_current_user(request, db)
    log_action(db, user=user, action="bulk_update", table="payments",
               description=f"Bulk set {updated} payment(s) to '{data.status}' for week {data.week_id}",
               new={"week_id": data.week_id, "status": data.status,
                    "member_count": updated, "method": data.payment_method})
    db.commit()
    return {"updated": updated}


@router.get("/batches/member/{member_id}")
def member_batches(member_id: int, limit: int = 50, db: Session = Depends(get_db)):
    batches = (db.query(PaymentBatch)
               .filter(PaymentBatch.member_id == member_id)
               .order_by(PaymentBatch.payment_date.desc())
               .limit(limit).all())
    return [batch_to_dict(b) for b in batches]


@router.get("/outstanding-members")
def outstanding_members(cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    """All members (active or received) that have unpaid past-due weeks in the given cycle."""
    now = _eat_now()

    if not cycle_id:
        active = db.query(Cycle).filter(Cycle.status == "active").order_by(Cycle.id.desc()).first()
        cycle_id = active.id if active else None

    q = (
        db.query(Member,
                 func.count(Payment.id).label("cnt"),
                 func.sum(Payment.amount).label("total"))
        .join(Payment, Payment.member_id == Member.id)
        .join(Week, Week.id == Payment.week_id)
        .filter(
            Member.status.in_(["active", "received"]),
            Payment.status.in_(["pending", "late", "missed"]),
            Week.draw_date <= now,
        )
    )
    if cycle_id:
        q = q.filter(Week.cycle_id == cycle_id)
    rows = q.group_by(Member.id).order_by(func.sum(Payment.amount).desc()).all()

    result = []
    for member, cnt, total in rows:
        spot_numbers = [sa.spot.number for sa in member.spot_assignments
                        if sa.is_active and (cycle_id is None or sa.cycle_id == cycle_id)]
        result.append({
            "member_id": member.id,
            "member_name": member.name,
            "phone": member.phone,
            "spot_numbers": spot_numbers,
            "status": member.status,
            "unpaid_count": int(cnt),
            "unpaid_amount": float(total),
        })
    return result


@router.get("/summary/cycle/{cycle_id}")
def cycle_payment_summary(cycle_id: int, db: Session = Depends(get_db)):
    weeks = db.query(Week).filter(Week.cycle_id == cycle_id).all()
    week_ids = [w.id for w in weeks]
    if not week_ids:
        return {"total_expected": 0, "total_paid": 0, "total_missed": 0,
                "collection_rate": 0, "weeks": []}

    all_payments = db.query(Payment).filter(Payment.week_id.in_(week_ids)).all()
    total_expected = sum(p.amount for p in all_payments)
    total_paid = sum(p.amount for p in all_payments if p.status == "paid")
    total_missed = sum(p.amount for p in all_payments if p.status == "missed")
    rate = (total_paid / total_expected * 100) if total_expected else 0

    week_summary = []
    for w in sorted(weeks, key=lambda x: x.week_number):
        wp = [p for p in all_payments if p.week_id == w.id]
        week_summary.append({
            "week_id": w.id,
            "week_number": w.week_number,
            "draw_date": w.draw_date.isoformat(),
            "is_group_week": w.is_group_week,
            "expected": sum(p.amount for p in wp),
            "paid": sum(p.amount for p in wp if p.status == "paid"),
            "pending": sum(1 for p in wp if p.status == "pending"),
            "missed": sum(1 for p in wp if p.status == "missed"),
            "late": sum(1 for p in wp if p.status == "late"),
        })

    return {
        "total_expected": total_expected,
        "total_paid": total_paid,
        "total_missed": total_missed,
        "collection_rate": round(rate, 1),
        "weeks": week_summary,
    }


@router.get("/daily-collection")
def daily_collection(date: Optional[str] = None, cycle_id: Optional[int] = None,
                     db: Session = Depends(get_db)):
    """Return all PaymentBatches recorded on a given date, grouped by payment method."""
    if date:
        try:
            target = _date.fromisoformat(date)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.")
    else:
        target = _date.today()

    batches = (db.query(PaymentBatch)
               .filter(func.date(PaymentBatch.payment_date) == target)
               .order_by(PaymentBatch.payment_method, PaymentBatch.id)
               .all())

    # Filter to only batches that contain payments for the given cycle
    if cycle_id:
        batches = [
            b for b in batches
            if any(p.week and p.week.cycle_id == cycle_id for p in b.payments)
        ]

    groups = {"cash": [], "bank_transfer": [], "cheque": []}
    totals = {"cash": 0.0, "bank_transfer": 0.0, "cheque": 0.0}

    for b in batches:
        method = b.payment_method if b.payment_method in groups else "cash"
        spot_numbers = (
            [sa.spot.number for sa in b.member.spot_assignments
             if sa.is_active and (cycle_id is None or sa.cycle_id == cycle_id)]
            if b.member else []
        )
        week_numbers = sorted([p.week.week_number for p in b.payments if p.week])
        entry = {
            "batch_id": b.id,
            "member_id": b.member_id,
            "member_name": b.member.name if b.member else "—",
            "spot_numbers": spot_numbers,
            "weeks_paid": b.weeks_paid,
            "week_numbers": week_numbers,
            "total_amount": float(b.total_amount),
            "reference": b.reference,
            "collected_by_name": b.collected_by.full_name if b.collected_by else None,
        }
        groups[method].append(entry)
        totals[method] += float(b.total_amount)

    grand_total = sum(totals.values())
    total_batches = sum(len(g) for g in groups.values())
    # Primary week = most recent week whose draw_date ≤ report date, scoped to cycle
    wq = db.query(Week).filter(func.date(Week.draw_date) <= target)
    if cycle_id:
        wq = wq.filter(Week.cycle_id == cycle_id)
    primary_week = wq.order_by(Week.draw_date.desc()).first()
    return {
        "date": target.isoformat(),
        "groups": groups,
        "totals": totals,
        "grand_total": grand_total,
        "total_batches": total_batches,
        "week_number": primary_week.week_number if primary_week else None,
    }


@router.get("/member/{member_id}/balance")
def member_payment_balance(member_id: int, up_to_week_number: int = 9999,
                           cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Check if a member is fully paid up to a given week number within the active cycle."""
    if not cycle_id:
        active = db.query(Cycle).filter(Cycle.status == "active").order_by(Cycle.id.desc()).first()
        cycle_id = active.id if active else None
    q = (db.query(Payment)
         .join(Week)
         .filter(Payment.member_id == member_id,
                 Payment.status.in_(["pending", "late", "missed"]),
                 Week.week_number <= up_to_week_number))
    if cycle_id:
        q = q.filter(Week.cycle_id == cycle_id)
    unpaid = q.all()
    return {
        "member_id": member_id,
        "fully_paid": len(unpaid) == 0,
        "unpaid_count": len(unpaid),
        "unpaid_amount": sum(p.amount for p in unpaid),
        "unpaid_weeks": sorted([p.week.week_number for p in unpaid]),
    }


# ── Payment Receipt ───────────────────────────────────────────────────────────

@router.get("/{payment_id}/receipt", response_class=HTMLResponse)
def payment_receipt(payment_id: int, db: Session = Depends(get_db)):
    p = (db.query(Payment)
           .options(
               selectinload(Payment.batch).selectinload(PaymentBatch.payments).selectinload(Payment.week),
               selectinload(Payment.collected_by),
               selectinload(Payment.week),
           )
           .filter(Payment.id == payment_id)
           .first())
    if not p:
        raise HTTPException(status_code=404, detail="Payment not found")
    gs = db.query(Settings).first()
    m = p.member
    w = p.week
    cycle = w.cycle if w else None
    group_name  = (gs.group_name  or "እቁብ") if gs else "እቁብ"
    group_tag   = (gs.group_tagline or "Equb Manager") if gs else "Equb Manager"

    paid_date_html = _dual_date(p.paid_date)
    draw_date_html = _dual_date(w.draw_date if w else None)
    amount_str     = f"{int(p.amount):,} ETB"
    penalty_str    = f"{int(p.penalty_amount):,} ETB" if p.penalty_amount else None
    total_str      = f"{int((p.amount or 0) + (p.penalty_amount or 0)):,} ETB"
    method_labels  = {"cash": "Cash", "bank_transfer": "Bank Transfer", "cheque": "Cheque"}
    method_str     = method_labels.get(p.payment_method or "", p.payment_method or "—")
    receipt_no     = _receipt_no(p)
    collected_by_str = p.collected_by.full_name if p.collected_by else "—"
    is_late        = bool(p.penalty_amount)

    # Spot number(s) for this member in this cycle
    if m and cycle:
        spot_rows = (db.query(MemberSpot)
                     .filter(MemberSpot.member_id == m.id,
                             MemberSpot.cycle_id == cycle.id,
                             MemberSpot.is_active == True).all())
        spot_labels = []
        for sa in spot_rows:
            lbl = f"#{sa.spot.number}" if sa.spot else "?"
            if sa.share == "half":
                lbl += " (½)"
            spot_labels.append(lbl)
        spot_str = ", ".join(spot_labels) if spot_labels else "—"
    else:
        spot_str = "—"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<title>Payment Receipt – {receipt_no}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Inter', sans-serif; background: #fff; color: #111; }}
  .page {{ max-width: 480px; margin: 40px auto; padding: 40px 32px; border: 1px solid #e5e7eb; border-radius: 16px; }}
  .header {{ text-align: center; margin-bottom: 28px; }}
  .logo {{ width: 56px; height: 56px; background: #078930; border-radius: 14px;
           display: inline-flex; align-items: center; justify-content: center;
           color: #fff; font-size: 26px; font-weight: 700; margin-bottom: 10px; }}
  .org {{ font-size: 20px; font-weight: 700; color: #111; }}
  .tag {{ font-size: 12px; color: #6b7280; margin-top: 2px; }}
  .badge {{ display: inline-block; background: #d1fae5; color: #065f46;
            font-size: 11px; font-weight: 600; border-radius: 20px;
            padding: 3px 12px; margin-top: 10px; letter-spacing: .4px; }}
  h2 {{ font-size: 15px; font-weight: 600; color: #374151; margin: 24px 0 12px; border-bottom: 1px solid #f3f4f6; padding-bottom: 8px; }}
  .row {{ display: flex; justify-content: space-between; padding: 7px 0; font-size: 14px; border-bottom: 1px solid #f9fafb; }}
  .row span:first-child {{ color: #6b7280; }}
  .row span:last-child {{ font-weight: 500; text-align: right; }}
  .total-row {{ display: flex; justify-content: space-between; padding: 10px 0 0; font-size: 15px; font-weight: 700; }}
  .penalty {{ color: #dc2626; font-size: 13px; }}
  .footer {{ text-align: center; font-size: 11px; color: #9ca3af; margin-top: 28px; border-top: 1px dashed #e5e7eb; padding-top: 16px; }}
  @media print {{
    body {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
    .no-print {{ display: none; }}
    .page {{ border: none; margin: 0; padding: 24px 20px; }}
  }}
</style>
</head>
<body>
<div class="page">
  <div class="header">
    <div class="logo">እ</div>
    <div class="org">{group_name}</div>
    <div class="tag">{group_tag}</div>
    <div class="badge">✓ Payment Received</div>
  </div>

  <h2>Receipt Details</h2>
  <div class="row"><span>Receipt #</span><span>{receipt_no}</span></div>
  <div class="row"><span>Date Paid</span>{paid_date_html}</div>
  <div class="row"><span>Payment Method</span><span>{method_str}</span></div>
  <div class="row"><span>Collected By</span><span>{collected_by_str}</span></div>
  {"<div class='row'><span>Reference</span><span>" + p.reference + "</span></div>" if p.reference else ""}
  {"<div class='row'><span>Status</span><span style='color:#dc2626;font-weight:600'>Late Payment</span></div>" if is_late else ""}

  <h2>Member</h2>
  <div class="row"><span>Name</span><span>{m.name if m else "—"}</span></div>
  <div class="row"><span>Phone</span><span>{m.phone if m and m.phone else "—"}</span></div>
  <div class="row"><span>Spot #</span><span>{spot_str}</span></div>

  <h2>Equb Details</h2>
  <div class="row"><span>Cycle</span><span>{cycle.name if cycle else "—"}</span></div>
  <div class="row"><span>Week #</span><span>{w.week_number if w else "—"}</span></div>
  <div class="row"><span>Draw Date</span>{draw_date_html}</div>

  <h2>Amount</h2>
  <div class="row"><span>Contribution</span><span>{amount_str}</span></div>
  {f'<div class="row penalty"><span>Late Penalty</span><span>{penalty_str}</span></div>' if penalty_str else ""}
  <div class="total-row"><span>Total</span><span>{total_str}</span></div>
  {"<div class='row' style='margin-top:12px;font-size:13px;color:#6b7280'><span>Notes</span><span style='text-align:right'>" + p.notes + "</span></div>" if p.notes else ""}

  <div class="footer">
    This receipt was generated by {group_name} · {group_tag}<br/>
    Generated on {datetime.utcnow().strftime("%d %b %Y %H:%M")} UTC
  </div>
</div>
<div class="no-print" style="text-align:center;margin:20px">
  <button onclick="window.print()"
    style="background:#078930;color:#fff;border:none;padding:10px 28px;border-radius:8px;font-size:14px;cursor:pointer;font-weight:600;">
    🖨 Print / Save as PDF
  </button>
</div>
</body>
</html>"""
    return HTMLResponse(content=html)

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import func
from pydantic import BaseModel, model_validator
from typing import Optional, List, Literal
from datetime import datetime, date as _date, timezone
from collections import defaultdict
from database import get_db, Payment, PaymentBatch, Member, MemberSpot, Week, Cycle
from routers.notifications import send_payment_confirmed


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)

router = APIRouter()

METHODS = {"cash", "bank_transfer", "cheque"}


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
                .join(Member).order_by(Member.name).all())
    return [payment_to_dict(p, cycle_id) for p in payments]


@router.get("/member/{member_id}")
def payments_for_member(member_id: int, cycle_id: Optional[int] = None,
                        db: Session = Depends(get_db)):
    if not cycle_id:
        active = db.query(Cycle).filter(Cycle.status == "active").first()
        cycle_id = active.id if active else None
    q = db.query(Payment).filter(Payment.member_id == member_id).join(Week)
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
    active_cycle = db.query(Cycle).filter(Cycle.status == "active").first()
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

    now = _utcnow()
    q = (db.query(Payment)
         .filter(Payment.member_id == member_id,
                 Payment.status.in_(["pending", "late", "missed"]))
         .join(Week))
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

    member = db.query(Member).filter(Member.id == data.member_id).first()
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")

    pay_date = datetime.fromisoformat(data.payment_date) if data.payment_date else _utcnow()

    payments = (db.query(Payment)
                .filter(Payment.member_id == data.member_id,
                        Payment.week_id.in_(data.week_ids)).all())
    if not payments:
        raise HTTPException(status_code=404, detail="No matching payment records found")

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

    db.commit()
    db.refresh(batch)
    sms_status = send_payment_confirmed(payments[0], db) if payments else "skipped"
    result = batch_to_dict(batch)
    result["sms_status"] = sms_status
    return result


@router.put("/{payment_id}")
def update_payment(payment_id: int, data: PaymentUpdate, request: Request, db: Session = Depends(get_db)):
    p = db.query(Payment).filter(Payment.id == payment_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Payment not found")

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
    if data.status == "paid" and was_unpaid:
        p.collected_by_id = getattr(request.state, "user_id", None)
    db.commit()
    sms_status = "skipped"
    if data.status == "paid" and was_unpaid:
        sms_status = send_payment_confirmed(p, db)
    result = payment_to_dict(p)
    result["sms_status"] = sms_status
    return result


@router.post("/bulk")
def bulk_update(data: BulkPayment, request: Request, db: Session = Depends(get_db)):
    paid_date = datetime.fromisoformat(data.paid_date) if data.paid_date else _utcnow()
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
                try:
                    send_payment_confirmed(p, db)
                except Exception:
                    pass
            updated += 1
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
    now = _utcnow()

    if not cycle_id:
        active = db.query(Cycle).filter(Cycle.status == "active").first()
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
        active = db.query(Cycle).filter(Cycle.status == "active").first()
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

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from database import get_db, Member, MemberSpot, Week, Payment, PaymentBatch, PotTransaction, Spot, Cycle, Settings, PotDisbursement, AssociationExpense, cycle_cfg
from routers.deps import _require_admin


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)

router = APIRouter()


def _calc_vendor_payments(db, disbs, cycle_id):
    """
    For each disbursement, calculate how much the vendor actually receives
    (only paid spots for that week, not all spots).
    Returns dict: {disbursement_id: {"vendor_payment": float, "vendor_paid_spots": int}}
    """
    if not disbs:
        return {}

    from sqlalchemy import func as _func
    from database import cycle_cfg as _ccfg

    # Get cycle config once
    cycle_obj = db.query(Cycle).filter(Cycle.id == cycle_id).first()
    gs = db.query(Settings).first()
    cfg = _ccfg(cycle_obj, gs) if cycle_obj else None
    if not cfg:
        return {d.id: {"vendor_payment": 0, "vendor_paid_spots": 0} for d in disbs}

    week_ids = [d.week_id for d in disbs]

    # Single query: all paid payments for those weeks
    paid_pmts = db.query(Payment).filter(
        Payment.week_id.in_(week_ids), Payment.status == "paid"
    ).all()

    # Group member_ids by week_id
    members_by_week: dict = {}
    for p in paid_pmts:
        members_by_week.setdefault(p.week_id, set()).add(p.member_id)

    # Single query: all active MemberSpots for relevant members in this cycle
    all_member_ids = {mid for mids in members_by_week.values() for mid in mids}
    if not all_member_ids:
        return {d.id: {"vendor_payment": 0, "vendor_paid_spots": 0} for d in disbs}

    ms_rows = db.query(MemberSpot).filter(
        MemberSpot.member_id.in_(all_member_ids),
        MemberSpot.cycle_id == cycle_id,
        MemberSpot.is_active == True,
    ).all()

    # Index MemberSpots by member_id
    ms_by_member: dict = {}
    for ms in ms_rows:
        ms_by_member.setdefault(ms.member_id, []).append(ms)

    result = {}
    for d in disbs:
        paid_members = members_by_week.get(d.week_id, set())
        vendor_payment = 0.0
        vendor_paid_spots = 0
        for mid in paid_members:
            for ms in ms_by_member.get(mid, []):
                vendor_paid_spots += 1
                vendor_payment += (cfg.full_spot_voucher if ms.share == "full"
                                   else cfg.half_spot_voucher)
        result[d.id] = {"vendor_payment": round(vendor_payment, 2),
                         "vendor_paid_spots": vendor_paid_spots}
    return result


@router.get("/dashboard")
def dashboard_stats(cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    gs = db.query(Settings).first()
    if cycle_id:
        cycle = db.query(Cycle).filter(Cycle.id == cycle_id).first()
    else:
        cycle = db.query(Cycle).filter(Cycle.status == "active").first()
    settings = gs  # kept for the association_fund_detail check below

    # Spot counts — scoped to the active cycle's configured range so stale rows
    # from a previous cycle with different spot counts don't inflate the total.
    if cycle:
        _cfg_spots = cycle_cfg(cycle, gs)
        _n_total   = (_cfg_spots.total_member_spots or 0) + (_cfg_spots.total_assoc_spots or 0)
        total_spots    = _n_total
        received_spots = db.query(Spot).filter(Spot.status == "received", Spot.number <= _n_total).count()
        active_spots   = db.query(Spot).filter(Spot.status == "active",   Spot.number <= _n_total).count()
    else:
        total_spots    = db.query(Spot).count()
        received_spots = db.query(Spot).filter(Spot.status == "received").count()
        active_spots   = db.query(Spot).filter(Spot.status == "active").count()

    # ── Cycle-scoped member counts ───────────────────────────────────────────
    if cycle:
        member_ids_in_cycle = [
            r[0] for r in db.query(MemberSpot.member_id).filter(
                MemberSpot.cycle_id == cycle.id,
                MemberSpot.is_active == True,
            ).distinct().all()
        ]
        members_in_cycle = db.query(Member).filter(Member.id.in_(member_ids_in_cycle)).all() if member_ids_in_cycle else []
        total_members    = len(members_in_cycle)
        active_members   = sum(1 for m in members_in_cycle if m.status == "active")
        received_members = sum(1 for m in members_in_cycle if m.status == "received")
        # Available spots = spots with room left for this cycle's memberships
        assigned_spot_ids = [
            r[0] for r in db.query(MemberSpot.spot_id).filter(
                MemberSpot.cycle_id == cycle.id, MemberSpot.is_active == True
            ).all()
        ]
        from collections import Counter
        spot_usage = Counter(assigned_spot_ids)
        # A spot has room if: no full-share member, and < 2 half-share members
        cycle_ms = db.query(MemberSpot).filter(
            MemberSpot.cycle_id == cycle.id, MemberSpot.is_active == True
        ).all()
        fully_taken_spot_ids = set()
        for ms in cycle_ms:
            if ms.share == "full":
                fully_taken_spot_ids.add(ms.spot_id)
        half_counts = Counter(ms.spot_id for ms in cycle_ms if ms.share == "half")
        for sid, cnt in half_counts.items():
            if cnt >= 2:
                fully_taken_spot_ids.add(sid)
        available_spots_count = db.query(Spot).filter(
            Spot.status == "active",
            ~Spot.id.in_(fully_taken_spot_ids) if fully_taken_spot_ids else True
        ).count()
    else:
        total_members    = db.query(Member).filter(Member.status != "left").count()
        active_members   = db.query(Member).filter(Member.status == "active").count()
        received_members = db.query(Member).filter(Member.status == "received").count()
        available_spots_count = active_spots

    next_week = None
    last_draw = None
    association_fund = 0
    total_collected = 0
    weeks_done = 0

    if cycle:
        next_week_obj = db.query(Week).filter(
            Week.cycle_id == cycle.id,
            Week.status == "pending"
        ).order_by(Week.week_number).first()

        last_draw_obj = db.query(Week).filter(
            Week.cycle_id == cycle.id,
            Week.status.in_(["drawn", "sold"])
        ).order_by(Week.week_number.desc()).first()

        weeks_done = db.query(Week).filter(
            Week.cycle_id == cycle.id,
            Week.status.in_(["drawn", "sold"])
        ).count()

        if next_week_obj:
            next_week = {
                "id": next_week_obj.id,
                "week_number": next_week_obj.week_number,
                "draw_date": next_week_obj.draw_date.isoformat(),
                "is_group_week": next_week_obj.is_group_week,
                "net_pot": next_week_obj.net_pot,
            }
        if last_draw_obj:
            winner = None
            if last_draw_obj.winner_spot:
                winner = ", ".join(sa.member.name for sa in last_draw_obj.winner_spot.spot_assignments
                                   if sa.is_active and sa.cycle_id == last_draw_obj.cycle_id)
            # check for sale transaction
            tx = last_draw_obj.transactions[0] if last_draw_obj.transactions else None
            last_draw = {
                "week_number": last_draw_obj.week_number,
                "draw_date": last_draw_obj.draw_date.isoformat(),
                "status": last_draw_obj.status,
                "winner": winner,
                "buyer": tx.buyer.name if tx and tx.buyer else None,
                "net_pot": last_draw_obj.net_pot,
            }

        paid = db.query(Payment).join(Week).filter(
            Week.cycle_id == cycle.id,
            Payment.status == "paid"
        ).all()
        total_collected = sum(p.amount for p in paid)
        # Association fund: from actual paid member payments (not theoretical week amounts)
        completed_weeks = db.query(Week).filter(
            Week.cycle_id == cycle.id,
            Week.status.in_(["drawn", "sold"])
        ).all()
        from routers.draws import _actual_assoc_collected
        association_fund = _actual_assoc_collected(
            db, [w.id for w in completed_weeks], cycle.id
        )

        # Service fee and voucher totals from disbursements for this cycle
        disb_week_ids = [w.id for w in completed_weeks]
        disbursements = db.query(PotDisbursement).filter(
            PotDisbursement.week_id.in_(disb_week_ids)
        ).all() if disb_week_ids else []
        total_service_fee = sum(d.service_fee or 0 for d in disbursements)
        total_voucher = sum(d.voucher_deduction or 0 for d in disbursements)
    else:
        total_service_fee = 0
        total_voucher = 0

    total_weeks = db.query(Week).filter(Week.cycle_id == cycle.id).count() if cycle else 0

    # ── Current week payment snapshot ───────────────────────────────────────
    current_week_stats = {"paid": 0, "pending": 0, "missed": 0, "total": 0, "collected": 0}
    if cycle and next_week_obj:
        wps = db.query(Payment).filter(Payment.week_id == next_week_obj.id).all()
        current_week_stats = {
            "paid":      sum(1 for p in wps if p.status == "paid"),
            "pending":   sum(1 for p in wps if p.status == "pending"),
            "missed":    sum(1 for p in wps if p.status == "missed"),
            "total":     len(wps),
            "collected": sum(p.amount for p in wps if p.status == "paid"),
        }

    # ── Debtors count (members with any past-due unpaid weeks) ──────────────
    debtors_count = 0
    if cycle:
        now_dt = _utcnow()
        debtors_count = (
            db.query(Payment.member_id)
            .join(Week, Week.id == Payment.week_id)
            .filter(
                Payment.status.in_(["pending", "late", "missed"]),
                Week.draw_date <= now_dt,
                Week.cycle_id == cycle.id,
            )
            .distinct()
            .count()
        )

    return {
        "total_spots": total_spots,
        "received_spots": received_spots,
        "active_spots": active_spots,
        "available_spots_count": available_spots_count,
        "total_members": total_members,
        "active_members": active_members,
        "received_members": received_members,
        "weeks_done": weeks_done,
        "total_weeks": total_weeks,
        "next_week": next_week,
        "last_draw": last_draw,
        "total_collected": total_collected,
        "association_fund": association_fund,
        "total_service_fee": total_service_fee,
        "total_voucher": total_voucher,
        "cycle": {
            "id": cycle.id,
            "name": cycle.name,
            "start_date": cycle.start_date.isoformat(),
            "draw_phase": cycle.draw_phase,
        } if cycle else None,
        "current_week": current_week_stats,
        "debtors_count": debtors_count,
        "settings": (lambda cfg: {
            "full_spot_amount": cfg.full_spot_amount,
            "half_spot_amount": cfg.half_spot_amount,
            "association_deduction": cfg.association_deduction,
            "full_spot_voucher": cfg.full_spot_voucher,
            "half_spot_voucher": cfg.half_spot_voucher,
            "total_member_spots": cfg.total_member_spots,
            "total_assoc_spots": cfg.total_assoc_spots,
        })(cycle_cfg(cycle, gs)) if gs else None,
    }


@router.get("/transactions")
def recent_transactions(limit: int = 20, cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    if not cycle_id:
        active = db.query(Cycle).filter(Cycle.status == "active").first()
        cycle_id = active.id if active else None
    q = db.query(PotTransaction)
    if cycle_id:
        q = q.join(Week).filter(Week.cycle_id == cycle_id)
    txs = q.order_by(PotTransaction.transaction_date.desc()).limit(limit).all()
    return [
        {
            "id": t.id,
            "week_id": t.week_id,
            "week_number": t.week.week_number if t.week else None,
            "type": t.transaction_type,
            "buyer": t.buyer.name if t.buyer else None,
            "seller": t.seller.name if t.seller else None,
            "percentage": t.percentage,
            "gross_amount": t.gross_amount,
            "seller_fee": t.seller_fee,
            "buyer_receives": t.buyer_receives,
            "date": t.transaction_date.isoformat(),
        }
        for t in txs
    ]


@router.get("/ledger")
def ledger(cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Chronological ledger: disbursements for a cycle."""
    if not cycle_id:
        cycle = db.query(Cycle).filter(Cycle.status == "active").first()
        cycle_id = cycle.id if cycle else None
    if not cycle_id:
        return []
    week_ids = [r[0] for r in db.query(Week.id).filter(Week.cycle_id == cycle_id).all()]
    if not week_ids:
        return []
    disbs = (db.query(PotDisbursement)
             .filter(PotDisbursement.week_id.in_(week_ids))
             .order_by(PotDisbursement.cheque_date).all())
    rows = []
    for d in disbs:
        winner_names = ", ".join(
            sa.member.name for sa in d.winner_spot.spot_assignments
            if sa.is_active and (d.week is None or sa.cycle_id == d.week.cycle_id)
        ) if d.winner_spot else "—"
        rows.append({
            "week_number": d.week.week_number if d.week else None,
            "cheque_date": d.cheque_date.isoformat(),
            "winner": winner_names,
            "gross_amount": d.gross_amount,
            "association_amount": d.week.association_amount if d.week else 0,
            "service_fee": d.service_fee or 0,
            "voucher_deduction": d.voucher_deduction or 0,
            "net_amount": d.net_amount,
            "cheque_number": d.cheque_number,
            "status": d.status,
        })
    return rows


@router.get("/weekly-summary/{week_id}")
def weekly_payment_summary(week_id: int, db: Session = Depends(get_db)):
    """Payment totals by method for a given week — actual cash received in that week's window."""
    from datetime import timedelta
    w = db.query(Week).filter(Week.id == week_id).first()
    if not w:
        raise HTTPException(status_code=404, detail="Week not found")

    # Determine this week's collection window: prev draw_date+1 → this draw_date
    prev_week = (db.query(Week)
                 .filter(Week.cycle_id == w.cycle_id, Week.week_number == w.week_number - 1)
                 .first())
    window_start = (prev_week.draw_date + timedelta(days=1)) if prev_week else (w.draw_date - timedelta(days=365))
    window_end = w.draw_date

    # All paid payments in the cycle whose paid_date falls in this window
    cycle_week_ids = [r[0] for r in db.query(Week.id).filter(Week.cycle_id == w.cycle_id).all()]
    all_paid = db.query(Payment).filter(
        Payment.week_id.in_(cycle_week_ids),
        Payment.status == "paid",
    ).all()

    payments = []
    for p in all_paid:
        if p.paid_date:
            pd = p.paid_date.date() if hasattr(p.paid_date, "date") else p.paid_date
        else:
            # No paid_date: attribute to the week it belongs to
            pw = db.query(Week).filter(Week.id == p.week_id).first()
            pd = pw.draw_date.date() if pw else window_end.date()
        if window_start.date() <= pd <= window_end.date():
            payments.append(p)

    totals = {"cash": 0.0, "bank_transfer": 0.0, "cheque": 0.0, "other": 0.0}
    count  = {"cash": 0,   "bank_transfer": 0,   "cheque": 0,   "other": 0}
    rows = []
    for p in payments:
        method = p.payment_method or "cash"
        bucket = method if method in totals else "other"
        totals[bucket] += p.amount
        count[bucket]  += 1
        rows.append({
            "member_id":   p.member_id,
            "member_name": p.member.name if p.member else "",
            "week_number": p.week.week_number if p.week else None,
            "amount":      p.amount,
            "method":      method,
            "reference":   p.reference,
            "paid_date":   p.paid_date.isoformat() if p.paid_date else None,
        })

    rows.sort(key=lambda r: (r["method"], r["member_name"]))
    return {
        "week_id":        week_id,
        "week_number":    w.week_number,
        "draw_date":      w.draw_date.isoformat(),
        "is_group_week":  w.is_group_week,
        "is_worker_week": bool(getattr(w, "is_worker_week", False)),
        "total_paid":     sum(totals.values()),
        "totals":         totals,
        "count":          count,
        "payments":       rows,
    }


@router.get("/association-fund")
def association_fund_detail(cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    if cycle_id:
        cycle = db.query(Cycle).filter(Cycle.id == cycle_id).first()
    else:
        cycle = db.query(Cycle).filter(Cycle.status == "active").first()
    if not cycle:
        return {"total": 0, "weeks": []}

    weeks = db.query(Week).filter(
        Week.cycle_id == cycle.id,
        Week.status.in_(["drawn", "sold"])
    ).order_by(Week.week_number).all()

    total = 0
    breakdown = []
    for w in weeks:
        amt = w.association_amount or 0
        total += amt
        breakdown.append({
            "week_number": w.week_number,
            "draw_date": w.draw_date.isoformat(),
            "amount": amt,
            "is_group_week": w.is_group_week,
        })

    return {"total": total, "weeks": breakdown}


@router.get("/balance-sheet")
def balance_sheet(cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Full financial position for a cycle."""
    if not cycle_id:
        cycle = db.query(Cycle).filter(Cycle.status == "active").first()
        if not cycle:
            return {}
        cycle_id = cycle.id

    week_ids = [r[0] for r in db.query(Week.id).filter(Week.cycle_id == cycle_id).all()]
    if not week_ids:
        return {
            "total_collected": 0, "total_cheques": 0,
            "total_voucher": 0, "voucher_paid": 0, "voucher_outstanding": 0,
            "total_service_fee": 0, "association_fund": 0,
            "association_expenses": 0, "association_balance": 0,
            "cash_balance": 0, "pending_draw_balance": 0,
        }

    # Cash IN — actual member payments
    paid_payments = db.query(Payment).filter(
        Payment.week_id.in_(week_ids), Payment.status == "paid"
    ).all()
    total_collected = sum(p.amount for p in paid_payments)

    # Disbursements
    disbs = db.query(PotDisbursement).filter(
        PotDisbursement.week_id.in_(week_ids)
    ).all()
    total_cheques      = sum(d.net_amount for d in disbs)
    total_voucher      = sum(d.voucher_deduction or 0 for d in disbs)
    total_service_fee  = sum(d.service_fee or 0 for d in disbs)

    # Vendor payment = only paid spots (not full deduction which assoc collects from winner)
    vendor_data = _calc_vendor_payments(db, disbs, cycle_id)
    total_vendor_payment   = sum(v["vendor_payment"] for v in vendor_data.values())
    voucher_paid_amt       = sum(vendor_data[d.id]["vendor_payment"] for d in disbs if d.voucher_paid)
    voucher_outstanding    = sum(vendor_data[d.id]["vendor_payment"] for d in disbs if not d.voucher_paid)
    assoc_retains_voucher  = total_voucher - total_vendor_payment

    # Association fund: from actual paid member payments
    completed_weeks = db.query(Week).filter(
        Week.cycle_id == cycle_id,
        Week.status.in_(["drawn", "sold"])
    ).all()
    from routers.draws import _actual_assoc_collected
    association_fund = _actual_assoc_collected(
        db, [w.id for w in completed_weeks], cycle_id
    )

    # Association expenses paid out
    expenses = db.query(AssociationExpense).filter(
        AssociationExpense.cycle_id == cycle_id
    ).all()
    association_expenses = sum(e.amount for e in expenses)
    association_balance  = association_fund - association_expenses

    # Cash held by association = In - Cheques - Vendor voucher payments - Expenses
    # (assoc retains the difference between full voucher deduction and vendor payment)
    cash_out = total_cheques + voucher_paid_amt + association_expenses
    cash_balance = total_collected - cash_out

    # Pending draw balance = what's been collected but not yet disbursed
    disbursed_week_ids = {d.week_id for d in disbs}
    pending_weeks = [wid for wid in week_ids if wid not in disbursed_week_ids]
    pending_draw_balance = sum(
        p.amount for p in paid_payments if p.week_id in pending_weeks
    )

    return {
        "total_collected":       total_collected,
        "total_cheques":         total_cheques,
        "total_voucher":         total_voucher,
        "total_vendor_payment":  total_vendor_payment,
        "assoc_retains_voucher": assoc_retains_voucher,
        "voucher_paid":          voucher_paid_amt,
        "voucher_outstanding":   voucher_outstanding,
        "total_service_fee":     total_service_fee,
        "association_fund":      association_fund,
        "association_expenses":  association_expenses,
        "association_balance":   association_balance,
        "cash_balance":          cash_balance,
        "pending_draw_balance":  pending_draw_balance,
        "disbursements_count":   len(disbs),
    }


@router.get("/vouchers")
def voucher_tracker(cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    """List of all disbursements with voucher tracking status."""
    if not cycle_id:
        cycle = db.query(Cycle).filter(Cycle.status == "active").first()
        if not cycle:
            return []
        cycle_id = cycle.id

    week_ids = [r[0] for r in db.query(Week.id).filter(Week.cycle_id == cycle_id).all()]
    if not week_ids:
        return []

    disbs = (db.query(PotDisbursement)
             .filter(PotDisbursement.week_id.in_(week_ids))
             .order_by(PotDisbursement.cheque_date).all())
    # Pre-fetch cycle config once
    cycle_obj = db.query(Cycle).filter(Cycle.id == cycle_id).first()
    gs = db.query(Settings).first()
    cfg = cycle_cfg(cycle_obj, gs) if cycle_obj else None

    rows = []
    for d in disbs:
        winner = ", ".join(
            sa.member.name for sa in d.winner_spot.spot_assignments
            if sa.is_active and (d.week is None or sa.cycle_id == d.week.cycle_id)
        ) if d.winner_spot else "—"

        # Vendor payment = only members who actually PAID that week
        vendor_payment = 0
        vendor_paid_spots = 0
        if cfg and d.week_id:
            paid_pmts = db.query(Payment).filter(
                Payment.week_id == d.week_id,
                Payment.status == "paid"
            ).all()
            for pmt in paid_pmts:
                for ms in db.query(MemberSpot).filter(
                    MemberSpot.member_id == pmt.member_id,
                    MemberSpot.cycle_id == cycle_id,
                    MemberSpot.is_active == True
                ).all():
                    vendor_paid_spots += 1
                    vendor_payment += (cfg.full_spot_voucher if ms.share == "full"
                                       else cfg.half_spot_voucher)

        voucher_deduction = d.voucher_deduction or 0
        rows.append({
            "id":                d.id,
            "week_number":       d.week.week_number if d.week else None,
            "cheque_date":       d.cheque_date.isoformat(),
            "winner":            winner,
            "voucher_deduction": voucher_deduction,
            "vendor_payment":    round(vendor_payment, 2),
            "vendor_paid_spots": vendor_paid_spots,
            "assoc_retains":     round(voucher_deduction - vendor_payment, 2),
            "voucher_paid":      bool(d.voucher_paid),
            "voucher_paid_date": d.voucher_paid_date.isoformat() if d.voucher_paid_date else None,
        })
    return rows


@router.put("/vouchers/{disbursement_id}/mark-paid")
def mark_voucher_paid(disbursement_id: int, db: Session = Depends(get_db)):
    """Mark a week's voucher amount as paid to the vendor."""
    d = db.query(PotDisbursement).filter(PotDisbursement.id == disbursement_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Disbursement not found")
    d.voucher_paid = True
    d.voucher_paid_date = _utcnow()
    db.commit()
    return {"ok": True, "voucher_paid_date": d.voucher_paid_date.isoformat()}


@router.put("/vouchers/{disbursement_id}/unmark-paid")
def unmark_voucher_paid(disbursement_id: int, db: Session = Depends(get_db)):
    d = db.query(PotDisbursement).filter(PotDisbursement.id == disbursement_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Disbursement not found")
    d.voucher_paid = False
    d.voucher_paid_date = None
    db.commit()
    return {"ok": True}


# ── Association Expenses ──────────────────────────────────────────────────────

class ExpenseCreate(BaseModel):
    description: str
    amount: float
    expense_date: Optional[str] = None
    notes: Optional[str] = None


@router.get("/association-expenses")
def list_expenses(cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    if not cycle_id:
        cycle = db.query(Cycle).filter(Cycle.status == "active").first()
        cycle_id = cycle.id if cycle else None
    if not cycle_id:
        return []
    exps = (db.query(AssociationExpense)
              .filter(AssociationExpense.cycle_id == cycle_id)
              .order_by(AssociationExpense.expense_date)
              .all())
    return [{"id": e.id, "description": e.description, "amount": e.amount,
             "expense_date": e.expense_date.isoformat() if e.expense_date else None,
             "notes": e.notes} for e in exps]


@router.post("/association-expenses")
def add_expense(data: ExpenseCreate, request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    cycle = db.query(Cycle).filter(Cycle.status == "active").first()
    if not cycle:
        raise HTTPException(404, "No active cycle")
    exp = AssociationExpense(
        cycle_id=cycle.id,
        description=data.description.strip(),
        amount=data.amount,
        expense_date=(datetime.fromisoformat(data.expense_date)
                      if data.expense_date else _utcnow()),
        notes=data.notes,
    )
    db.add(exp)
    db.commit()
    db.refresh(exp)
    return {"id": exp.id, "description": exp.description, "amount": exp.amount,
            "expense_date": exp.expense_date.isoformat() if exp.expense_date else None,
            "notes": exp.notes}


@router.delete("/association-expenses/{expense_id}")
def delete_expense(expense_id: int, request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    exp = db.query(AssociationExpense).filter(AssociationExpense.id == expense_id).first()
    if not exp:
        raise HTTPException(404, "Expense not found")
    db.delete(exp)
    db.commit()
    return {"ok": True}


@router.get("/general-ledger")
def general_ledger(cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Chronological ledger of every financial event for a cycle."""
    if not cycle_id:
        cycle = db.query(Cycle).filter(Cycle.status == "active").first()
        if not cycle:
            return []
        cycle_id = cycle.id

    week_ids = [r[0] for r in db.query(Week.id).filter(Week.cycle_id == cycle_id).all()]
    if not week_ids:
        return []

    entries = []

    # 1. Member collections — grouped by paid_date into the week window it was received in
    from datetime import datetime as _dt, timedelta as _td
    _today = _dt.utcnow().date()
    weeks = db.query(Week).filter(Week.cycle_id == cycle_id).order_by(Week.week_number).all()
    week_map = {w.id: w for w in weeks}
    sorted_weeks = sorted(weeks, key=lambda w: w.draw_date)

    # Build per-week collection windows: prev_draw_date+1 → this_draw_date
    windows = []
    for i, w in enumerate(sorted_weeks):
        start = (sorted_weeks[i - 1].draw_date.date() + _td(days=1)) if i > 0 else w.draw_date.date() - _td(days=365)
        windows.append((w, start, w.draw_date.date()))

    payments = db.query(Payment).filter(
        Payment.week_id.in_(week_ids), Payment.status == "paid"
    ).all()
    by_collection_week: dict = {}
    for p in payments:
        # Use paid_date when available, fall back to the week's draw_date
        if p.paid_date:
            pd = p.paid_date.date() if hasattr(p.paid_date, "date") else p.paid_date
        else:
            wk = week_map.get(p.week_id)
            pd = wk.draw_date.date() if wk else _today
        for w, ws, we in windows:
            if ws <= pd <= we:
                by_collection_week[w.id] = by_collection_week.get(w.id, 0) + p.amount
                break
        else:
            # paid after all draw windows — attribute to the last past week
            for w, ws, we in reversed(windows):
                if we <= _today:
                    by_collection_week[w.id] = by_collection_week.get(w.id, 0) + p.amount
                    break

    for w in sorted_weeks:
        total = by_collection_week.get(w.id, 0)
        if total == 0 or w.draw_date.date() > _today:
            continue
        entries.append({
            "date":        w.draw_date.isoformat(),
            "type":        "collection",
            "description": f"Week {w.week_number} — Cash Collected",
            "credit":      total,
            "debit":       0,
        })

    # 2. Winner cheques (disbursements)
    disbs = (db.query(PotDisbursement)
             .filter(PotDisbursement.week_id.in_(week_ids))
             .order_by(PotDisbursement.cheque_date).all())
    gl_vendor_data = _calc_vendor_payments(db, disbs, cycle_id)
    for d in disbs:
        winner = ", ".join(
            sa.member.name for sa in d.winner_spot.spot_assignments
            if sa.is_active and (d.week is None or sa.cycle_id == d.week.cycle_id)
        ) if d.winner_spot else "—"
        wk = week_map.get(d.week_id)
        entries.append({
            "date":        d.cheque_date.isoformat(),
            "type":        "cheque",
            "description": f"Week {wk.week_number if wk else '?'} — Cheque #{d.cheque_number} → {winner}",
            "credit":      0,
            "debit":       d.net_amount,
        })
        # Voucher payment (if paid to vendor) — only the vendor's portion (paid spots)
        if d.voucher_paid and d.voucher_deduction:
            vp = gl_vendor_data.get(d.id, {}).get("vendor_payment", d.voucher_deduction)
            entries.append({
                "date":        (d.voucher_paid_date or d.cheque_date).isoformat(),
                "type":        "voucher",
                "description": f"Week {wk.week_number if wk else '?'} — Voucher Paid to Vendor",
                "credit":      0,
                "debit":       vp,
            })

    # 3. Association expenses
    expenses = db.query(AssociationExpense).filter(
        AssociationExpense.cycle_id == cycle_id
    ).order_by(AssociationExpense.expense_date).all()
    for e in expenses:
        entries.append({
            "date":        e.expense_date.isoformat(),
            "type":        "expense",
            "description": f"Association Expense — {e.description}",
            "credit":      0,
            "debit":       e.amount,
        })

    # Sort chronologically and add running balance
    entries.sort(key=lambda x: x["date"])
    balance = 0.0
    for e in entries:
        balance = balance + e["credit"] - e["debit"]
        e["balance"] = balance

    return entries


@router.get("/cycle-distribution")
def cycle_distribution(cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    """
    Calculate end-of-cycle profit distribution to members.
    Pool = Association Fund Net + Assoc Spot Sale Profits + Voucher Retained by Association.
    Split by spot weight: full spot = 1.0, half spot = 0.5.
    """
    if not cycle_id:
        cycle = db.query(Cycle).filter(Cycle.status == "active").first()
        if not cycle:
            return {"total_distributable": 0, "breakdown": {}, "per_unit_amount": 0, "members": []}
        cycle_id = cycle.id

    week_ids = [r[0] for r in db.query(Week.id).filter(Week.cycle_id == cycle_id).all()]

    # ── Association Fund Net ──────────────────────────────────────────────────
    completed_weeks = db.query(Week).filter(
        Week.cycle_id == cycle_id, Week.status.in_(["drawn", "sold"])
    ).all()
    from routers.draws import _actual_assoc_collected
    association_fund = _actual_assoc_collected(db, [w.id for w in completed_weeks], cycle_id)
    expenses = db.query(AssociationExpense).filter(AssociationExpense.cycle_id == cycle_id).all()
    association_expenses = sum(e.amount for e in expenses)
    association_balance = association_fund - association_expenses

    # ── Association Spot Sale Profits ─────────────────────────────────────────
    assoc_spot_profit = 0.0
    if week_ids:
        assoc_txs = db.query(PotTransaction).filter(
            PotTransaction.week_id.in_(week_ids),
            PotTransaction.transaction_type == "assoc_spot_sale",
        ).all()
        assoc_spot_profit = sum(t.seller_fee or 0 for t in assoc_txs)

    # ── Voucher Retained by Association ──────────────────────────────────────
    disbs = db.query(PotDisbursement).filter(
        PotDisbursement.week_id.in_(week_ids)
    ).all() if week_ids else []
    total_voucher = sum(d.voucher_deduction or 0 for d in disbs)
    vendor_data = _calc_vendor_payments(db, disbs, cycle_id)
    total_vendor_payment = sum(v["vendor_payment"] for v in vendor_data.values())
    voucher_retained = total_voucher - total_vendor_payment

    total_distributable = association_balance + assoc_spot_profit + voucher_retained

    # ── Member weights ────────────────────────────────────────────────────────
    memberships = db.query(MemberSpot).filter(
        MemberSpot.cycle_id == cycle_id,
        MemberSpot.is_active == True,
    ).all()
    # Only member spots (not association spots)
    member_spot_ids = {
        ms.spot_id for ms in memberships
        if ms.spot and ms.spot.spot_type == "member"
    }
    member_ms = [ms for ms in memberships if ms.spot_id in member_spot_ids]

    # Group by member
    from collections import defaultdict
    weight_by_member: dict = defaultdict(float)
    spots_by_member: dict = defaultdict(list)
    for ms in member_ms:
        w = 1.0 if ms.share == "full" else 0.5
        weight_by_member[ms.member_id] += w
        spots_by_member[ms.member_id].append({
            "spot_number": ms.spot.number if ms.spot else None,
            "share": ms.share,
            "weight": w,
        })

    total_weight = sum(weight_by_member.values())
    per_unit = (total_distributable / total_weight) if total_weight else 0.0

    # Fetch member details
    member_ids = list(weight_by_member.keys())
    members = {m.id: m for m in db.query(Member).filter(Member.id.in_(member_ids)).all()}

    rows = []
    for mid, weight in sorted(weight_by_member.items(), key=lambda x: -x[1]):
        m = members.get(mid)
        if not m:
            continue
        rows.append({
            "member_id":   mid,
            "member_name": m.name,
            "phone":       m.phone,
            "spots":       sorted(spots_by_member[mid], key=lambda s: s["spot_number"] or 0),
            "weight":      weight,
            "amount":      round(per_unit * weight, 2),
        })

    return {
        "total_distributable": round(total_distributable, 2),
        "breakdown": {
            "association_fund":     round(association_fund, 2),
            "association_expenses": round(association_expenses, 2),
            "association_balance":  round(association_balance, 2),
            "assoc_spot_profit":    round(assoc_spot_profit, 2),
            "voucher_retained":     round(voucher_retained, 2),
        },
        "total_weight":   total_weight,
        "per_unit_amount": round(per_unit, 2),
        "members":        rows,
    }


@router.get("/collection-trend")
def collection_trend(cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Week-by-week actual cash collected, using the same paid_date windowing as the General Ledger."""
    from datetime import datetime as _dt, timedelta as _td
    if not cycle_id:
        c = db.query(Cycle).filter(Cycle.status == "active").first()
        if not c:
            return []
        cycle_id = c.id
    weeks = db.query(Week).filter(Week.cycle_id == cycle_id).order_by(Week.week_number).all()
    if not weeks:
        return []
    week_ids = [w.id for w in weeks]
    today = _dt.utcnow().date()

    # Build per-week date windows: prev_draw_date+1 → this_draw_date
    sorted_weeks = sorted(weeks, key=lambda w: w.draw_date)
    windows = []
    for i, w in enumerate(sorted_weeks):
        start = (sorted_weeks[i - 1].draw_date.date() + _td(days=1)) if i > 0 else w.draw_date.date() - _td(days=365)
        windows.append((w, start, w.draw_date.date()))

    # Fetch all paid payments once
    all_paid = db.query(Payment).filter(
        Payment.week_id.in_(week_ids), Payment.status == "paid"
    ).all()
    week_map = {w.id: w for w in weeks}

    # Bucket each payment into a collection window by paid_date
    collected: dict = {}
    for p in all_paid:
        if p.paid_date:
            pd = p.paid_date.date() if hasattr(p.paid_date, "date") else p.paid_date
        else:
            wk = week_map.get(p.week_id)
            pd = wk.draw_date.date() if wk else today
        for w, ws, we in windows:
            if ws <= pd <= we:
                collected[w.id] = collected.get(w.id, 0) + p.amount
                break
        else:
            for w, ws, we in reversed(windows):
                if we <= today:
                    collected[w.id] = collected.get(w.id, 0) + p.amount
                    break

    # Per-week obligation: sum payments by week_id regardless of paid_date
    all_week_payments = db.query(Payment).filter(Payment.week_id.in_(week_ids)).all()
    obligation_paid: dict = {}
    obligation_counts: dict = {}
    obligation_total: dict = {}
    for p in all_week_payments:
        obligation_total[p.week_id] = obligation_total.get(p.week_id, 0) + p.amount
        if p.status == "paid":
            obligation_paid[p.week_id] = obligation_paid.get(p.week_id, 0) + p.amount
            obligation_counts[p.week_id] = obligation_counts.get(p.week_id, 0) + 1

    # paid_count per week_id for cash-flow tooltip
    paid_counts: dict = {}
    for p in all_paid:
        paid_counts[p.week_id] = paid_counts.get(p.week_id, 0) + 1

    result = []
    for w in sorted_weeks:
        if w.draw_date.date() > today:
            result.append({
                "week_number":    w.week_number,
                "draw_date":      w.draw_date.isoformat(),
                "is_group_week":  w.is_group_week,
                "week_status":    w.status,
                "paid":           0.0,
                "paid_count":     0,
                "total":          0.0,
                "cash_collected": 0.0,
                "obligation_paid": 0.0,
                "obligation_total": float(obligation_total.get(w.id, 0)),
            })
            continue
        result.append({
            "week_number":    w.week_number,
            "draw_date":      w.draw_date.isoformat(),
            "is_group_week":  w.is_group_week,
            "week_status":    w.status,
            "paid":           float(collected.get(w.id, 0)),
            "paid_count":     paid_counts.get(w.id, 0),
            "total":          float(collected.get(w.id, 0)),
            "cash_collected": float(collected.get(w.id, 0)),
            "obligation_paid": float(obligation_paid.get(w.id, 0)),
            "obligation_total": float(obligation_total.get(w.id, 0)),
        })
    return result


@router.get("/member-ranking")
def member_ranking(cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Members ranked by payment consistency (on-time rate)."""
    from collections import defaultdict
    if not cycle_id:
        c = db.query(Cycle).filter(Cycle.status == "active").first()
        if not c:
            return []
        cycle_id = c.id

    # Single query for all cycle memberships (spots per member)
    memberships = db.query(MemberSpot).filter(
        MemberSpot.cycle_id == cycle_id, MemberSpot.is_active == True
    ).all()
    if not memberships:
        return []
    member_ids = list({ms.member_id for ms in memberships})
    spots_by_member: dict = defaultdict(list)
    for ms in memberships:
        if ms.spot:
            spots_by_member[ms.member_id].append(ms.spot.number)

    # Single query for all members
    members = {m.id: m for m in db.query(Member).filter(Member.id.in_(member_ids)).all()}

    # Single query for all completed-week payments
    completed_week_ids = [r[0] for r in db.query(Week.id).filter(
        Week.cycle_id == cycle_id, Week.status.in_(["drawn", "sold"])
    ).all()]

    payments_by_member: dict = defaultdict(list)
    if completed_week_ids:
        for p in db.query(Payment).filter(
            Payment.member_id.in_(member_ids),
            Payment.week_id.in_(completed_week_ids),
        ).all():
            payments_by_member[p.member_id].append(p.status)

    result = []
    for mid in member_ids:
        member = members.get(mid)
        if not member:
            continue
        statuses = payments_by_member[mid]
        paid   = statuses.count("paid")
        missed = statuses.count("missed")
        late   = statuses.count("late")
        total  = len(statuses)
        rate   = round(paid / total * 100, 1) if total else 100.0
        result.append({
            "member_id":    mid,
            "member_name":  member.name,
            "phone":        member.phone,
            "spot_numbers": sorted(spots_by_member[mid]),
            "paid_weeks":   paid,
            "missed_weeks": missed,
            "late_weeks":   late,
            "total_weeks":  total,
            "rate":         rate,
        })
    return sorted(result, key=lambda x: (-x["rate"], -x["paid_weeks"]))


@router.get("/member/{member_id}/statement")
def member_statement(member_id: int, cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Full payment history for one member (for printable statement)."""
    member = db.query(Member).filter(Member.id == member_id).first()
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")

    q = db.query(Payment).filter(Payment.member_id == member_id).join(Week)
    if cycle_id:
        q = q.filter(Week.cycle_id == cycle_id)
    ps = q.order_by(Week.week_number).all()

    spot_numbers = [sa.spot.number for sa in member.spot_assignments
                    if sa.is_active and (cycle_id is None or sa.cycle_id == cycle_id)]

    # ── Batch payment history (grouped by payment event) ─────────────────────
    batches = (db.query(PaymentBatch)
               .filter(PaymentBatch.member_id == member_id)
               .order_by(PaymentBatch.payment_date).all())
    batch_rows = []
    for b in batches:
        bp = b.payments
        if cycle_id:
            bp = [p for p in bp if p.week and p.week.cycle_id == cycle_id]
        if not bp:
            continue
        week_numbers = sorted([p.week.week_number for p in bp if p.week])
        batch_rows.append({
            "payment_date":     b.payment_date.isoformat(),
            "weeks_covered":    week_numbers,
            "weeks_count":      len(week_numbers),
            "total_amount":     float(sum(p.amount for p in bp)),
            "payment_method":   b.payment_method,
            "reference":        b.reference,
            "collected_by_name": b.collected_by.full_name if b.collected_by else None,
        })
    # Payments marked paid directly (no batch)
    for p in ps:
        if p.status == "paid" and not p.batch_id and p.week:
            batch_rows.append({
                "payment_date":     p.paid_date.isoformat() if p.paid_date else None,
                "weeks_covered":    [p.week.week_number],
                "weeks_count":      1,
                "total_amount":     float(p.amount),
                "payment_method":   p.payment_method,
                "reference":        p.reference,
                "collected_by_name": p.collected_by.full_name if p.collected_by else None,
            })
    batch_rows.sort(key=lambda x: x["payment_date"] or "")

    # ── Unpaid / outstanding weeks ────────────────────────────────────────────
    # Only show weeks up to the most recent drawn/sold week — exclude future pending weeks
    from datetime import datetime as _dt
    _today = _dt.utcnow().date()
    unpaid = [
        {
            "week_number": p.week.week_number,
            "draw_date":   p.week.draw_date.isoformat(),
            "status":      p.status,
            "amount":      float(p.amount),
        }
        for p in ps
        if p.status in ("pending", "missed", "late")
        and p.week
        and p.week.draw_date.date() <= _today
    ]

    return {
        "member_id":    member.id,
        "member_name":  member.name,
        "phone":        member.phone,
        "spot_numbers": spot_numbers,
        "batches":      batch_rows,
        "unpaid":       unpaid,
        "summary": {
            "paid":                sum(1 for p in ps if p.status == "paid"),
            "missed":              sum(1 for p in ps if p.status == "missed"),
            "late":                sum(1 for p in ps if p.status == "late"),
            "pending":             sum(1 for p in ps if p.status == "pending"),
            "total_paid_amount":   float(sum(p.amount for p in ps if p.status == "paid")),
            "total_owed_amount":   float(sum(p.amount for p in unpaid)),
        },
    }

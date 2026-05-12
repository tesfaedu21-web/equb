from fastapi import APIRouter, Depends, HTTPException
from typing import Optional
from datetime import datetime
from sqlalchemy.orm import Session
from database import get_db, Member, MemberSpot, Week, Payment, PaymentBatch, PotTransaction, Spot, Cycle, Settings, PotDisbursement, AssociationExpense

router = APIRouter()


@router.get("/dashboard")
def dashboard_stats(cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    settings = db.query(Settings).first()
    if cycle_id:
        cycle = db.query(Cycle).filter(Cycle.id == cycle_id).first()
    else:
        cycle = db.query(Cycle).filter(Cycle.status == "active").first()

    # Spot counts (global — spot status reflects active cycle state)
    total_spots = db.query(Spot).count()
    received_spots = db.query(Spot).filter(Spot.status == "received").count()
    active_spots = db.query(Spot).filter(Spot.status == "active").count()

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
                winner = ", ".join(sa.member.name for sa in last_draw_obj.winner_spot.spot_assignments if sa.is_active)
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
        # Association fund: sum actual association_amount from each drawn/sold week
        completed_weeks = db.query(Week).filter(
            Week.cycle_id == cycle.id,
            Week.status.in_(["drawn", "sold"])
        ).all()
        association_fund = sum(w.association_amount or 0 for w in completed_weeks)

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
        from datetime import datetime as _dt
        now_dt = _dt.utcnow()
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
        } if cycle else None,
        "current_week": current_week_stats,
        "debtors_count": debtors_count,
        "settings": {
            "full_spot_amount": settings.full_spot_amount,
            "half_spot_amount": settings.half_spot_amount,
            "association_deduction": settings.association_deduction,
            "full_spot_voucher": settings.full_spot_voucher,
            "half_spot_voucher": settings.half_spot_voucher,
            "total_member_spots": settings.total_member_spots,
            "total_assoc_spots": settings.total_assoc_spots,
        } if settings else None,
    }


@router.get("/transactions")
def recent_transactions(limit: int = 20, cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
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
            sa.member.name for sa in d.winner_spot.spot_assignments if sa.is_active
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
    """Payment totals by method for a given week — for printable summary."""
    w = db.query(Week).filter(Week.id == week_id).first()
    if not w:
        raise HTTPException(status_code=404, detail="Week not found")

    payments = db.query(Payment).filter(
        Payment.week_id == week_id,
        Payment.status == "paid",
    ).all()

    totals = {"cash": 0.0, "bank_transfer": 0.0, "cheque": 0.0, "other": 0.0}
    count = {"cash": 0, "bank_transfer": 0, "cheque": 0, "other": 0}
    rows = []
    for p in payments:
        method = p.payment_method or "cash"
        bucket = method if method in totals else "other"
        totals[bucket] += p.amount
        count[bucket] += 1
        rows.append({
            "member_id": p.member_id,
            "member_name": p.member.name if p.member else "",
            "amount": p.amount,
            "method": method,
            "reference": p.reference,
            "paid_date": p.paid_date.isoformat() if p.paid_date else None,
        })

    return {
        "week_id": week_id,
        "week_number": w.week_number,
        "draw_date": w.draw_date.isoformat(),
        "is_group_week": w.is_group_week,
        "is_worker_week": bool(getattr(w, "is_worker_week", False)),
        "total_paid": sum(totals.values()),
        "totals": totals,
        "count": count,
        "payments": rows,
    }


@router.get("/association-fund")
def association_fund_detail(cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    settings = db.query(Settings).first()
    if cycle_id:
        cycle = db.query(Cycle).filter(Cycle.id == cycle_id).first()
    else:
        cycle = db.query(Cycle).filter(Cycle.status == "active").first()
    if not cycle or not settings:
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
    voucher_paid_amt   = sum(d.voucher_deduction or 0 for d in disbs if d.voucher_paid)
    voucher_outstanding= total_voucher - voucher_paid_amt
    total_service_fee  = sum(d.service_fee or 0 for d in disbs)

    # Association fund (from drawn/sold weeks)
    completed_weeks = db.query(Week).filter(
        Week.cycle_id == cycle_id,
        Week.status.in_(["drawn", "sold"])
    ).all()
    association_fund = sum(w.association_amount or 0 for w in completed_weeks)

    # Association expenses paid out
    expenses = db.query(AssociationExpense).filter(
        AssociationExpense.cycle_id == cycle_id
    ).all()
    association_expenses = sum(e.amount for e in expenses)
    association_balance  = association_fund - association_expenses

    # Cash held by association = In - Cheques - Vouchers paid - Expenses
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
    rows = []
    for d in disbs:
        winner = ", ".join(
            sa.member.name for sa in d.winner_spot.spot_assignments if sa.is_active
        ) if d.winner_spot else "—"
        rows.append({
            "id":                d.id,
            "week_number":       d.week.week_number if d.week else None,
            "cheque_date":       d.cheque_date.isoformat(),
            "winner":            winner,
            "voucher_deduction": d.voucher_deduction or 0,
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
    d.voucher_paid_date = datetime.utcnow()
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

    # 1. Member collections per week
    weeks = db.query(Week).filter(Week.cycle_id == cycle_id).order_by(Week.week_number).all()
    week_map = {w.id: w for w in weeks}
    payments = db.query(Payment).filter(
        Payment.week_id.in_(week_ids), Payment.status == "paid"
    ).all()
    by_week = {}
    for p in payments:
        by_week.setdefault(p.week_id, 0)
        by_week[p.week_id] += p.amount
    for wid, total in sorted(by_week.items(), key=lambda x: week_map[x[0]].week_number):
        w = week_map[wid]
        entries.append({
            "date":        w.draw_date.isoformat(),
            "type":        "collection",
            "description": f"Week {w.week_number} — Member Contributions",
            "credit":      total,
            "debit":       0,
        })

    # 2. Winner cheques (disbursements)
    disbs = (db.query(PotDisbursement)
             .filter(PotDisbursement.week_id.in_(week_ids))
             .order_by(PotDisbursement.cheque_date).all())
    for d in disbs:
        winner = ", ".join(
            sa.member.name for sa in d.winner_spot.spot_assignments if sa.is_active
        ) if d.winner_spot else "—"
        wk = week_map.get(d.week_id)
        entries.append({
            "date":        d.cheque_date.isoformat(),
            "type":        "cheque",
            "description": f"Week {wk.week_number if wk else '?'} — Cheque #{d.cheque_number} → {winner}",
            "credit":      0,
            "debit":       d.net_amount,
        })
        # Voucher payment (if paid to vendor)
        if d.voucher_paid and d.voucher_deduction:
            entries.append({
                "date":        (d.voucher_paid_date or d.cheque_date).isoformat(),
                "type":        "voucher",
                "description": f"Week {wk.week_number if wk else '?'} — Voucher Paid to Vendor",
                "credit":      0,
                "debit":       d.voucher_deduction,
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


@router.get("/collection-trend")
def collection_trend(cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Week-by-week collection totals for the trend chart."""
    if not cycle_id:
        c = db.query(Cycle).filter(Cycle.status == "active").first()
        if not c:
            return []
        cycle_id = c.id
    weeks = db.query(Week).filter(Week.cycle_id == cycle_id).order_by(Week.week_number).all()
    result = []
    for w in weeks:
        ps = db.query(Payment).filter(Payment.week_id == w.id).all()
        result.append({
            "week_number": w.week_number,
            "draw_date": w.draw_date.isoformat(),
            "is_group_week": w.is_group_week,
            "week_status": w.status,
            "paid":         float(sum(p.amount for p in ps if p.status == "paid")),
            "missed":       float(sum(p.amount for p in ps if p.status == "missed")),
            "paid_count":   sum(1 for p in ps if p.status == "paid"),
            "missed_count": sum(1 for p in ps if p.status == "missed"),
            "pending_count":sum(1 for p in ps if p.status == "pending"),
            "total":        float(sum(p.amount for p in ps)),
        })
    return result


@router.get("/member-ranking")
def member_ranking(cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Members ranked by payment consistency (on-time rate)."""
    if not cycle_id:
        c = db.query(Cycle).filter(Cycle.status == "active").first()
        if not c:
            return []
        cycle_id = c.id

    member_ids = [r[0] for r in db.query(MemberSpot.member_id).filter(
        MemberSpot.cycle_id == cycle_id, MemberSpot.is_active == True
    ).distinct().all()]

    completed_week_ids = [r[0] for r in db.query(Week.id).filter(
        Week.cycle_id == cycle_id, Week.status.in_(["drawn", "sold"])
    ).all()]

    result = []
    for mid in member_ids:
        member = db.query(Member).filter(Member.id == mid).first()
        if not member:
            continue
        ps = db.query(Payment).filter(
            Payment.member_id == mid,
            Payment.week_id.in_(completed_week_ids),
        ).all() if completed_week_ids else []
        paid   = sum(1 for p in ps if p.status == "paid")
        missed = sum(1 for p in ps if p.status in ["missed"])
        late   = sum(1 for p in ps if p.status == "late")
        total  = len(ps)
        rate   = round(paid / total * 100, 1) if total else 100.0
        spot_numbers = [sa.spot.number for sa in member.spot_assignments
                        if sa.is_active and sa.cycle_id == cycle_id]
        result.append({
            "member_id":   mid,
            "member_name": member.name,
            "phone":       member.phone,
            "spot_numbers": spot_numbers,
            "paid_weeks":  paid,
            "missed_weeks": missed,
            "late_weeks":  late,
            "total_weeks": total,
            "rate":        rate,
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
    unpaid = [
        {
            "week_number": p.week.week_number,
            "draw_date":   p.week.draw_date.isoformat(),
            "status":      p.status,
            "amount":      float(p.amount),
        }
        for p in ps if p.status in ("pending", "missed", "late") and p.week
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
            "total_owed_amount":   float(sum(p.amount for p in ps if p.status in ("missed", "late", "pending"))),
        },
    }

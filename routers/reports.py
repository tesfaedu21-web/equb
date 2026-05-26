from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from sqlalchemy import func
from database import get_db, Member, MemberSpot, Week, Payment, PaymentBatch, PotTransaction, Spot, Cycle, Settings, PotDisbursement, AssociationExpense, DistributionCheque, VoucherReturn, cycle_cfg
from routers.deps import _require_feature


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)

router = APIRouter()


def _calc_issued_counts(db, week_ids, cycle_id):
    """
    For each week, count full/half vouchers issued to members who paid.
    One voucher per spot per paying member (full spot → full card, half → half card).
    """
    if not week_ids:
        return {}

    all_ms = (db.query(MemberSpot)
              .filter(MemberSpot.cycle_id == cycle_id, MemberSpot.is_active == True)
              .all())
    ms_by_member: dict = {}
    for ms in all_ms:
        ms_by_member.setdefault(ms.member_id, []).append(ms)

    paid = (db.query(Payment)
            .filter(Payment.week_id.in_(week_ids), Payment.status == "paid")
            .all())
    paying_by_week: dict = {}
    for p in paid:
        paying_by_week.setdefault(p.week_id, set()).add(p.member_id)

    result = {}
    for wid in week_ids:
        full_c = half_c = 0
        for mid in paying_by_week.get(wid, set()):
            for ms in ms_by_member.get(mid, []):
                if ms.share == "full":
                    full_c += 1
                else:
                    half_c += 1
        result[wid] = {"full_issued": full_c, "half_issued": half_c}
    return result


def _calc_vendor_payments(db, disbs, cycle_id):
    """
    For each disbursement, calculate vendor payment from VoucherReturn records when available,
    falling back to paid-member counts. Used by balance sheet and general ledger.
    Returns dict: {disbursement_id: {"vendor_payment": float, "vendor_paid_spots": int}}
    """
    if not disbs:
        return {}

    from database import cycle_cfg as _ccfg

    cycle_obj = db.query(Cycle).filter(Cycle.id == cycle_id).first()
    gs = db.query(Settings).first()
    cfg = _ccfg(cycle_obj, gs) if cycle_obj else None
    if not cfg:
        return {d.id: {"vendor_payment": 0, "vendor_paid_spots": 0} for d in disbs}

    week_ids = [d.week_id for d in disbs]
    issued = _calc_issued_counts(db, week_ids, cycle_id)

    # Load VoucherReturn records for these weeks
    vr_rows = db.query(VoucherReturn).filter(VoucherReturn.week_id.in_(week_ids)).all()
    returns_by_week = {vr.week_id: vr for vr in vr_rows}

    result = {}
    for d in disbs:
        vr = returns_by_week.get(d.week_id)
        if vr is not None:
            full_c = vr.full_count
            half_c = vr.half_count
        else:
            iss = issued.get(d.week_id, {"full_issued": 0, "half_issued": 0})
            full_c = iss["full_issued"]
            half_c = iss["half_issued"]
        vendor_payment = full_c * cfg.full_spot_voucher + half_c * cfg.half_spot_voucher
        result[d.id] = {
            "vendor_payment": round(vendor_payment, 2),
            "vendor_paid_spots": full_c + half_c,
        }
    return result


@router.get("/dashboard")
def dashboard_stats(cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    gs = db.query(Settings).first()
    if cycle_id:
        cycle = db.query(Cycle).filter(Cycle.id == cycle_id).first()
    else:
        cycle = db.query(Cycle).filter(Cycle.status == "active").order_by(Cycle.id.desc()).first()
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
        # Association fund: deductions are collected when members pay, not when the week is drawn
        from routers.draws import _actual_assoc_collected
        _all_cycle_weeks   = db.query(Week).filter(Week.cycle_id == cycle.id).all()
        all_cycle_week_ids = [w.id for w in _all_cycle_weeks]
        completed_weeks    = [w for w in _all_cycle_weeks if w.status in ("drawn", "sold")]
        association_fund   = _actual_assoc_collected(db, all_cycle_week_ids, cycle.id)

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

    # ── Debtors count + top debtors ─────────────────────────────────────────
    debtors_count = 0
    debtors_total_owed = 0.0
    top_debtors = []
    if cycle:
        now_dt = _utcnow()
        debtor_rows = (
            db.query(
                Payment.member_id,
                Member.name,
                func.count(Payment.id).label("unpaid_weeks"),
                func.sum(Payment.amount + func.coalesce(Payment.penalty_amount, 0)).label("total_owed"),
            )
            .join(Week, Week.id == Payment.week_id)
            .join(Member, Member.id == Payment.member_id)
            .filter(
                Payment.status.in_(["pending", "late", "missed"]),
                Week.draw_date <= now_dt,
                Week.cycle_id == cycle.id,
            )
            .group_by(Payment.member_id, Member.name)
            .order_by(func.sum(Payment.amount + func.coalesce(Payment.penalty_amount, 0)).desc())
            .limit(5)
            .all()
        )
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
        debtors_total_owed = sum(float(r.total_owed or 0) for r in debtor_rows)
        top_debtors = [
            {"name": r.name, "unpaid_weeks": r.unpaid_weeks, "total_owed": round(float(r.total_owed or 0), 2)}
            for r in debtor_rows
        ]

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
        "debtors_total_owed": debtors_total_owed,
        "top_debtors": top_debtors,
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
        active = db.query(Cycle).filter(Cycle.status == "active").order_by(Cycle.id.desc()).first()
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
        cycle = db.query(Cycle).filter(Cycle.status == "active").order_by(Cycle.id.desc()).first()
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
    """Payment breakdown for a given week — paid vs pending/late members."""
    w = db.query(Week).filter(Week.id == week_id).first()
    if not w:
        raise HTTPException(status_code=404, detail="Week not found")

    all_payments = (
        db.query(Payment)
        .filter(Payment.week_id == week_id)
        .order_by(Payment.member_id)
        .all()
    )

    totals = {"cash": 0.0, "bank_transfer": 0.0, "cheque": 0.0, "other": 0.0}
    count  = {"cash": 0,   "bank_transfer": 0,   "cheque": 0,   "other": 0}
    paid_rows = []
    unpaid_rows = []

    for p in all_payments:
        member_name = p.member.name if p.member else ""
        if p.status == "paid":
            method = p.payment_method or "cash"
            bucket = method if method in totals else "other"
            totals[bucket] += p.amount
            count[bucket]  += 1
            paid_rows.append({
                "member_id":   p.member_id,
                "member_name": member_name,
                "week_number": w.week_number,
                "amount":      p.amount,
                "method":      method,
                "reference":   p.reference,
                "paid_date":   p.paid_date.isoformat() if p.paid_date else None,
            })
        else:
            unpaid_rows.append({
                "member_id":   p.member_id,
                "member_name": member_name,
                "status":      p.status,
                "amount":      p.amount,
            })

    paid_rows.sort(key=lambda r: (r["method"], r["member_name"]))
    unpaid_rows.sort(key=lambda r: r["member_name"])

    return {
        "week_id":        week_id,
        "week_number":    w.week_number,
        "draw_date":      w.draw_date.isoformat(),
        "is_group_week":  w.is_group_week,
        "is_worker_week": bool(getattr(w, "is_worker_week", False)),
        "total_paid":     sum(totals.values()),
        "totals":         totals,
        "count":          count,
        "payments":       paid_rows,
        "unpaid_count":   len(unpaid_rows),
        "unpaid":         unpaid_rows,
    }


@router.get("/association-fund")
def association_fund_detail(cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    if cycle_id:
        cycle = db.query(Cycle).filter(Cycle.id == cycle_id).first()
    else:
        cycle = db.query(Cycle).filter(Cycle.status == "active").order_by(Cycle.id.desc()).first()
    if not cycle:
        return {"total": 0, "expenses_total": 0, "balance": 0, "events": []}

    weeks = db.query(Week).filter(
        Week.cycle_id == cycle.id,
        Week.status.in_(["drawn", "sold"])
    ).order_by(Week.week_number).all()

    expenses = (db.query(AssociationExpense)
                .filter(AssociationExpense.cycle_id == cycle.id)
                .order_by(AssociationExpense.expense_date)
                .all())

    # Build chronological event list for running balance
    events = []
    for w in weeks:
        amt = w.association_amount or 0
        events.append({
            "type": "collection",
            "week_number": w.week_number,
            "draw_date": w.draw_date.isoformat(),
            "description": f"Week {w.week_number}" + (" (Group)" if w.is_group_week else ""),
            "amount": amt,
            "is_group_week": w.is_group_week,
        })
    for e in expenses:
        events.append({
            "type": "expense",
            "date": e.expense_date.isoformat() if e.expense_date else None,
            "description": e.description,
            "amount": -e.amount,
        })
    # Sort by date
    def _sort_key(ev):
        return ev.get("draw_date") or ev.get("date") or ""
    events.sort(key=_sort_key)

    # Add running balance
    balance = 0.0
    for ev in events:
        balance += ev["amount"]
        ev["running_balance"] = round(balance, 2)

    total_collected = sum(ev["amount"] for ev in events if ev["type"] == "collection")
    expenses_total  = sum(abs(ev["amount"]) for ev in events if ev["type"] == "expense")

    return {
        "total": round(total_collected, 2),
        "expenses_total": round(expenses_total, 2),
        "balance": round(total_collected - expenses_total, 2),
        "events": events,
    }


@router.get("/balance-sheet")
def balance_sheet(cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Full financial position for a cycle."""
    if not cycle_id:
        cycle = db.query(Cycle).filter(Cycle.status == "active").order_by(Cycle.id.desc()).first()
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

    # Association fund: deductions are collected when members pay, not when the week is drawn
    from routers.draws import _actual_assoc_collected
    all_cycle_week_ids = [w.id for w in db.query(Week).filter(Week.cycle_id == cycle_id).all()]
    association_fund = _actual_assoc_collected(db, all_cycle_week_ids, cycle_id)

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
    """List all completed weeks (drawn + sold/group) with voucher issued/returned counts."""
    if not cycle_id:
        cycle = db.query(Cycle).filter(Cycle.status == "active").order_by(Cycle.id.desc()).first()
        if not cycle:
            return []
        cycle_id = cycle.id

    # All completed weeks (drawn or sold), including group weeks — ordered by week_number
    completed_weeks = (db.query(Week)
                       .filter(Week.cycle_id == cycle_id,
                               Week.status.in_(["drawn", "sold"]))
                       .order_by(Week.week_number)
                       .all())
    if not completed_weeks:
        return []

    week_ids = [w.id for w in completed_weeks]
    weeks_by_id = {w.id: w for w in completed_weeks}

    disbs = (db.query(PotDisbursement)
             .filter(PotDisbursement.week_id.in_(week_ids))
             .all())

    cycle_obj = db.query(Cycle).filter(Cycle.id == cycle_id).first()
    gs = db.query(Settings).first()
    cfg = cycle_cfg(cycle_obj, gs) if cycle_obj else None

    issued = _calc_issued_counts(db, week_ids, cycle_id) if cfg else {}
    vr_rows = db.query(VoucherReturn).filter(VoucherReturn.week_id.in_(week_ids)).all()
    returns_by_week = {vr.week_id: vr for vr in vr_rows}

    # Group disbursements by week (half-spot wins produce two records per week)
    from collections import defaultdict
    week_disbs: dict = defaultdict(list)
    for d in disbs:
        week_disbs[d.week_id].append(d)

    rows = []
    for w in completed_weeks:
        wid = w.id
        group = week_disbs.get(wid, [])

        iss = issued.get(wid, {"full_issued": 0, "half_issued": 0})
        vr = returns_by_week.get(wid)

        full_returned = vr.full_count if vr is not None else None
        half_returned = vr.half_count if vr is not None else None

        if cfg and vr is not None:
            vendor_payment = (vr.full_count * cfg.full_spot_voucher
                              + vr.half_count * cfg.half_spot_voucher)
        elif cfg:
            vendor_payment = (iss["full_issued"] * cfg.full_spot_voucher
                              + iss["half_issued"] * cfg.half_spot_voucher)
        else:
            vendor_payment = 0

        # Sum voucher_deduction from disbursements; fall back to issued×rate for group weeks
        voucher_deduction = sum(x.voucher_deduction or 0 for x in group)
        if voucher_deduction == 0 and cfg and (iss["full_issued"] or iss["half_issued"]):
            voucher_deduction = round(
                iss["full_issued"] * cfg.full_spot_voucher
                + iss["half_issued"] * cfg.half_spot_voucher, 2
            )
        all_paid = bool(group) and all(bool(x.voucher_paid) for x in group)
        # voucher_paid: disbursements all paid OR (no disbursements → use VoucherReturn.vendor_paid)
        if group:
            all_paid = all(bool(x.voucher_paid) for x in group)
            paid_date = max(
                (x.voucher_paid_date for x in group if x.voucher_paid_date),
                default=None
            )
        else:
            all_paid = bool(vr and vr.vendor_paid)
            paid_date = vr.vendor_paid_date if (vr and vr.vendor_paid_date) else None

        ref_date = group[0].cheque_date if group else w.draw_date

        rows.append({
            "id":                group[0].id if group else None,
            "disbursement_ids":  [x.id for x in group],
            "week_id":           wid,
            "week_number":       w.week_number,
            "cheque_date":       ref_date.isoformat(),
            "voucher_deduction": voucher_deduction,
            "full_issued":       iss["full_issued"],
            "half_issued":       iss["half_issued"],
            "full_returned":     full_returned,
            "half_returned":     half_returned,
            "return_recorded":   vr is not None,
            "vendor_payment":    round(vendor_payment, 2),
            "assoc_retains":     round(voucher_deduction - vendor_payment, 2),
            "voucher_paid":      all_paid,
            "voucher_paid_date": paid_date.isoformat() if paid_date else None,
        })
    return rows


@router.put("/vouchers/week/{week_id}/mark-paid")
def mark_voucher_paid(week_id: int, request: Request, db: Session = Depends(get_db)):
    _require_feature(request, db, "view_reports")
    now = _utcnow()
    disbs = db.query(PotDisbursement).filter(PotDisbursement.week_id == week_id).all()
    if disbs:
        for d in disbs:
            d.voucher_paid = True
            d.voucher_paid_date = now
    else:
        # No disbursement (e.g. group week) — track via VoucherReturn
        vr = db.query(VoucherReturn).filter(VoucherReturn.week_id == week_id).first()
        if not vr:
            vr = VoucherReturn(week_id=week_id, full_count=0, half_count=0)
            db.add(vr)
        vr.vendor_paid = True
        vr.vendor_paid_date = now
    db.commit()
    return {"ok": True, "voucher_paid_date": now.isoformat()}


@router.put("/vouchers/week/{week_id}/unmark-paid")
def unmark_voucher_paid(week_id: int, request: Request, db: Session = Depends(get_db)):
    _require_feature(request, db, "view_reports")
    disbs = db.query(PotDisbursement).filter(PotDisbursement.week_id == week_id).all()
    if disbs:
        for d in disbs:
            d.voucher_paid = False
            d.voucher_paid_date = None
    else:
        vr = db.query(VoucherReturn).filter(VoucherReturn.week_id == week_id).first()
        if vr:
            vr.vendor_paid = False
            vr.vendor_paid_date = None
    db.commit()
    return {"ok": True}


class VoucherReturnIn(BaseModel):
    full_count: int = Field(..., ge=0)
    half_count: int = Field(..., ge=0)
    notes: Optional[str] = None


@router.post("/voucher-returns/{week_id}")
def record_voucher_return(week_id: int, data: VoucherReturnIn,
                          request: Request, db: Session = Depends(get_db)):
    _require_feature(request, db, "view_reports")
    """Record (or update) how many full/half voucher cards the vendor returned for a week."""
    w = db.query(Week).filter(Week.id == week_id).first()
    if not w:
        raise HTTPException(status_code=404, detail="Week not found")
    vr = db.query(VoucherReturn).filter(VoucherReturn.week_id == week_id).first()
    if vr:
        vr.full_count = data.full_count
        vr.half_count = data.half_count
        vr.notes = data.notes
        vr.recorded_at = _utcnow()
    else:
        vr = VoucherReturn(
            week_id=week_id,
            full_count=data.full_count,
            half_count=data.half_count,
            notes=data.notes,
        )
        db.add(vr)
    db.commit()
    return {"ok": True, "week_id": week_id,
            "full_count": vr.full_count, "half_count": vr.half_count}


@router.delete("/voucher-returns/{week_id}")
def delete_voucher_return(week_id: int, request: Request, db: Session = Depends(get_db)):
    _require_feature(request, db, "view_reports")
    """Remove a voucher return record for a week."""
    vr = db.query(VoucherReturn).filter(VoucherReturn.week_id == week_id).first()
    if not vr:
        raise HTTPException(status_code=404, detail="No return recorded for this week")
    db.delete(vr)
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
        cycle = db.query(Cycle).filter(Cycle.status == "active").order_by(Cycle.id.desc()).first()
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
    _require_feature(request, db, "view_reports")
    cycle = db.query(Cycle).filter(Cycle.status == "active").order_by(Cycle.id.desc()).first()
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
    _require_feature(request, db, "view_reports")
    exp = db.query(AssociationExpense).filter(AssociationExpense.id == expense_id).first()
    if not exp:
        raise HTTPException(404, "Expense not found")
    db.delete(exp)
    db.commit()
    return {"ok": True}


@router.get("/general-ledger")
def general_ledger(request: Request, cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    _require_feature(request, db, "view_reports")
    """Chronological ledger of every financial event for a cycle."""
    if not cycle_id:
        cycle = db.query(Cycle).filter(Cycle.status == "active").order_by(Cycle.id.desc()).first()
        if not cycle:
            return []
        cycle_id = cycle.id

    week_ids = [r[0] for r in db.query(Week.id).filter(Week.cycle_id == cycle_id).all()]
    if not week_ids:
        return []

    entries = []

    # 1. Member collections — one entry per week, dated by earliest paid_date (or draw_date)
    weeks = db.query(Week).filter(Week.cycle_id == cycle_id).order_by(Week.week_number).all()
    week_map = {w.id: w for w in weeks}

    payments = db.query(Payment).filter(
        Payment.week_id.in_(week_ids), Payment.status == "paid"
    ).all()

    by_week: dict = {}
    for p in payments:
        by_week.setdefault(p.week_id, []).append(p)

    for w in weeks:
        week_payments = by_week.get(w.id, [])
        total = sum(p.amount for p in week_payments)
        if total == 0:
            continue
        paid_dates = [p.paid_date for p in week_payments if p.paid_date]
        entry_date = min(paid_dates).date() if paid_dates else w.draw_date.date()
        entries.append({
            "date":        entry_date.isoformat(),
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
        cycle = db.query(Cycle).filter(Cycle.status == "active").order_by(Cycle.id.desc()).first()
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

    # ── Association Spot Sale + Group Week Sale Profits ───────────────────────
    assoc_spot_profit = 0.0
    if week_ids:
        assoc_txs = db.query(PotTransaction).filter(
            PotTransaction.week_id.in_(week_ids),
            PotTransaction.transaction_type.in_(["assoc_spot_sale", "group_week_sale"]),
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
    from datetime import timedelta as _td
    if not cycle_id:
        c = db.query(Cycle).filter(Cycle.status == "active").order_by(Cycle.id.desc()).first()
        if not c:
            return []
        cycle_id = c.id
    weeks = db.query(Week).filter(Week.cycle_id == cycle_id).order_by(Week.week_number).all()
    if not weeks:
        return []
    week_ids = [w.id for w in weeks]
    today = _utcnow().date()

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

    # Renderable week IDs: non-pending weeks with draw_date in the past
    renderable_ids = {
        w.id for w in sorted_weeks
        if w.status != "pending" and w.draw_date.date() <= today
    }

    # Bucket each payment into a collection window by paid_date.
    # If the matched window belongs to a pending/future week (excluded from chart),
    # fall back to the payment's obligation week so collections aren't lost.
    collected: dict = {}
    for p in all_paid:
        if p.paid_date:
            pd = p.paid_date.date() if hasattr(p.paid_date, "date") else p.paid_date
        else:
            wk = week_map.get(p.week_id)
            pd = wk.draw_date.date() if wk else today
        matched_id = None
        for w, ws, we in windows:
            if ws <= pd <= we:
                matched_id = w.id
                break
        # Use matched window only if it's a renderable (completed) week
        if matched_id and matched_id in renderable_ids:
            target = matched_id
        elif p.week_id in renderable_ids:
            target = p.week_id  # fall back to obligation week
        else:
            # last renderable week
            target = next((w.id for w, _, _ in reversed(windows) if w.id in renderable_ids), None)
        if target:
            collected[target] = collected.get(target, 0) + p.amount

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
        gross = float(w.gross_pot or 0)
        if w.draw_date.date() > today:
            result.append({
                "week_number":     w.week_number,
                "draw_date":       w.draw_date.isoformat(),
                "is_group_week":   w.is_group_week,
                "week_status":     w.status,
                "gross_pot":       gross,
                "paid":            0.0,
                "paid_count":      0,
                "total":           0.0,
                "cash_collected":  0.0,
                "obligation_paid": 0.0,
                "obligation_total": float(obligation_total.get(w.id, 0)),
            })
            continue
        oblig_paid = float(obligation_paid.get(w.id, 0))
        result.append({
            "week_number":     w.week_number,
            "draw_date":       w.draw_date.isoformat(),
            "is_group_week":   w.is_group_week,
            "week_status":     w.status,
            "gross_pot":       gross,
            "paid":            float(collected.get(w.id, 0)),
            "paid_count":      paid_counts.get(w.id, 0),
            "total":           float(collected.get(w.id, 0)),
            "cash_collected":  float(collected.get(w.id, 0)),
            "obligation_paid": oblig_paid,
            "obligation_total": float(obligation_total.get(w.id, 0)),
            "collection_rate": round(oblig_paid / gross * 100, 1) if gross else 0,
        })
    return result


@router.get("/member-ranking")
def member_ranking(cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Members ranked by payment consistency (on-time rate)."""
    from collections import defaultdict
    if not cycle_id:
        c = db.query(Cycle).filter(Cycle.status == "active").order_by(Cycle.id.desc()).first()
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
            "total_owed_amount":   float(sum(p["amount"] for p in unpaid)),
        },
    }


@router.get("/member/{member_id}/history")
def member_history(member_id: int, db: Session = Depends(get_db)):
    """Cross-cycle participation history for a single member."""
    member = db.query(Member).filter(Member.id == member_id).first()
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")

    # All cycles this member participated in
    cycle_ids = [
        r[0] for r in db.query(MemberSpot.cycle_id)
        .filter(MemberSpot.member_id == member_id)
        .distinct().all()
    ]
    cycles = db.query(Cycle).filter(Cycle.id.in_(cycle_ids)).order_by(Cycle.id).all()

    cycle_rows = []
    for cycle in cycles:
        # Spots in this cycle
        sas = [sa for sa in member.spot_assignments
               if sa.cycle_id == cycle.id and sa.is_active]
        spot_numbers = [sa.spot.number for sa in sas if sa.spot]
        shares = list({sa.share for sa in sas})

        # Payments in this cycle
        week_ids = [w.id for w in db.query(Week.id).filter(Week.cycle_id == cycle.id)]
        payments = db.query(Payment).filter(
            Payment.member_id == member_id,
            Payment.week_id.in_(week_ids),
        ).all() if week_ids else []

        total_weeks = len(week_ids)
        paid_count   = sum(1 for p in payments if p.status == "paid")
        missed_count = sum(1 for p in payments if p.status == "missed")
        late_count   = sum(1 for p in payments if p.status == "late")
        total_paid_amount = float(sum(p.amount for p in payments if p.status == "paid"))

        # Pot wins (PotTransaction where buyer_id = member)
        wins = db.query(PotTransaction).filter(
            PotTransaction.week_id.in_(week_ids),
            PotTransaction.buyer_id == member_id,
        ).all() if week_ids else []
        # Also direct wins (winner_spot is member's spot)
        direct_wins = []
        if week_ids:
            member_spot_ids = [sa.spot_id for sa in sas if sa.spot_id]
            direct_wins = db.query(Week).filter(
                Week.id.in_(week_ids),
                Week.winner_spot_id.in_(member_spot_ids),
                Week.status.in_(["drawn", "sold"]),
            ).all() if member_spot_ids else []

        pot_wins = []
        for w in direct_wins:
            tx = next((t for t in wins if t.week_id == w.id), None)
            pot_wins.append({
                "week_number":   w.week_number,
                "draw_date":     w.draw_date.isoformat(),
                "week_status":   w.status,
                "buyer_receives": float(tx.buyer_receives) if tx else float(w.net_pot or 0),
                "via_sale":      bool(tx),
            })

        # Marketplace sales where member was buyer (no direct spot win)
        marketplace_buys = []
        if week_ids:
            mkt_txs = db.query(PotTransaction).filter(
                PotTransaction.week_id.in_(week_ids),
                PotTransaction.buyer_id == member_id,
                PotTransaction.transaction_type.in_(["member_sale", "group_week_sale", "assoc_spot_sale"]),
            ).all()
            direct_win_week_ids = {w.id for w in direct_wins}
            for tx in mkt_txs:
                if tx.week_id not in direct_win_week_ids:
                    w = next((wk for wk in db.query(Week).filter(Week.id == tx.week_id).all()), None)
                    marketplace_buys.append({
                        "week_number":   w.week_number if w else None,
                        "draw_date":     w.draw_date.isoformat() if w else None,
                        "buyer_receives": float(tx.buyer_receives),
                        "transaction_type": tx.transaction_type,
                    })

        cycle_rows.append({
            "cycle_id":      cycle.id,
            "cycle_name":    cycle.name,
            "cycle_status":  cycle.status,
            "start_date":    cycle.start_date.isoformat() if cycle.start_date else None,
            "end_date":      cycle.end_date.isoformat() if cycle.end_date else None,
            "spot_numbers":  spot_numbers,
            "shares":        shares,
            "total_weeks":   total_weeks,
            "paid_weeks":    paid_count,
            "missed_weeks":  missed_count,
            "late_weeks":    late_count,
            "payment_rate":  round(paid_count / total_weeks * 100, 1) if total_weeks else 0,
            "total_paid_amount": total_paid_amount,
            "pot_wins":      pot_wins,
            "marketplace_buys": marketplace_buys,
        })

    return {
        "member_id":   member.id,
        "member_name": member.name,
        "phone":       member.phone,
        "status":      member.status,
        "cycles":      cycle_rows,
        "totals": {
            "cycles_participated": len(cycle_rows),
            "total_paid_amount":   sum(r["total_paid_amount"] for r in cycle_rows),
            "total_pot_wins":      sum(len(r["pot_wins"]) for r in cycle_rows),
        },
    }


# ── Distribution Cheques ──────────────────────────────────────────────────────

def _fmt_cheque(c: DistributionCheque) -> dict:
    return {
        "id":             c.id,
        "cycle_id":       c.cycle_id,
        "member_id":      c.member_id,
        "member_name":    c.member.name if c.member else None,
        "amount":         c.amount,
        "cheque_number":  c.cheque_number,
        "cheque_date":    c.cheque_date.isoformat(),
        "status":         c.status,
        "collected_at":   c.collected_at.isoformat() if c.collected_at else None,
        "notes":          c.notes,
    }


class ChequeCreate(BaseModel):
    cycle_id: int
    member_id: int
    amount: float
    cheque_number: str
    cheque_date: str
    notes: Optional[str] = None


@router.get("/distribution-cheques")
def list_distribution_cheques(cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    if not cycle_id:
        cycle = db.query(Cycle).filter(Cycle.status == "active").order_by(Cycle.id.desc()).first()
        cycle_id = cycle.id if cycle else None
    if not cycle_id:
        return []
    cheques = (db.query(DistributionCheque)
               .filter(DistributionCheque.cycle_id == cycle_id)
               .order_by(DistributionCheque.cheque_date)
               .all())
    return [_fmt_cheque(c) for c in cheques]


@router.post("/distribution-cheques")
def create_distribution_cheque(data: ChequeCreate, request: Request, db: Session = Depends(get_db)):
    _require_feature(request, db, "view_reports")
    existing = db.query(DistributionCheque).filter(
        DistributionCheque.cycle_id == data.cycle_id,
        DistributionCheque.member_id == data.member_id,
    ).first()
    if existing:
        raise HTTPException(400, "Cheque already issued for this member in this cycle")
    c = DistributionCheque(
        cycle_id=data.cycle_id,
        member_id=data.member_id,
        amount=data.amount,
        cheque_number=data.cheque_number.strip(),
        cheque_date=datetime.fromisoformat(data.cheque_date),
        notes=data.notes,
        created_at=_utcnow(),
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return _fmt_cheque(c)


@router.put("/distribution-cheques/{cheque_id}/collect")
def collect_distribution_cheque(cheque_id: int, request: Request, db: Session = Depends(get_db)):
    _require_feature(request, db, "view_reports")
    c = db.query(DistributionCheque).filter(DistributionCheque.id == cheque_id).first()
    if not c:
        raise HTTPException(404, "Cheque not found")
    c.status = "collected"
    c.collected_at = _utcnow()
    db.commit()
    return _fmt_cheque(c)


@router.put("/distribution-cheques/{cheque_id}/uncollect")
def uncollect_distribution_cheque(cheque_id: int, request: Request, db: Session = Depends(get_db)):
    _require_feature(request, db, "view_reports")
    c = db.query(DistributionCheque).filter(DistributionCheque.id == cheque_id).first()
    if not c:
        raise HTTPException(404, "Cheque not found")
    c.status = "issued"
    c.collected_at = None
    db.commit()
    return _fmt_cheque(c)


@router.delete("/distribution-cheques/{cheque_id}")
def delete_distribution_cheque(cheque_id: int, request: Request, db: Session = Depends(get_db)):
    _require_feature(request, db, "view_reports")
    c = db.query(DistributionCheque).filter(DistributionCheque.id == cheque_id).first()
    if not c:
        raise HTTPException(404, "Cheque not found")
    db.delete(c)
    db.commit()
    return {"ok": True}


# ── Cycle Closure Report ──────────────────────────────────────────────────────

@router.get("/cycle-closure")
def cycle_closure_report(cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    """
    End-of-cycle summary: all financial totals in one printable view.
    Suitable for presenting to committee at cycle close.
    """
    if not cycle_id:
        cycle = db.query(Cycle).filter(Cycle.status == "active").order_by(Cycle.id.desc()).first()
        if not cycle:
            return {}
        cycle_id = cycle.id

    cycle = db.query(Cycle).filter(Cycle.id == cycle_id).first()
    if not cycle:
        raise HTTPException(404, "Cycle not found")

    gs = db.query(Settings).first()
    cfg = cycle_cfg(cycle, gs)

    week_ids = [r[0] for r in db.query(Week.id).filter(Week.cycle_id == cycle_id).all()]
    weeks = db.query(Week).filter(Week.cycle_id == cycle_id).order_by(Week.week_number).all()
    total_weeks = len(weeks)
    drawn_weeks = [w for w in weeks if w.status in ("drawn", "sold")]

    # Member counts
    memberships = db.query(MemberSpot).filter(
        MemberSpot.cycle_id == cycle_id, MemberSpot.is_active == True
    ).all()
    member_ids = list({ms.member_id for ms in memberships})
    total_members = len(member_ids)
    full_spots = sum(1 for ms in memberships if ms.share == "full")
    half_spots = sum(1 for ms in memberships if ms.share == "half")

    # Collections
    paid_payments = db.query(Payment).filter(
        Payment.week_id.in_(week_ids), Payment.status == "paid"
    ).all() if week_ids else []
    total_collected = sum(p.amount for p in paid_payments)
    missed_payments = db.query(Payment).filter(
        Payment.week_id.in_(week_ids), Payment.status.in_(["missed", "late"])
    ).all() if week_ids else []
    total_missed = sum(p.amount for p in missed_payments)

    # Disbursements
    disbs = db.query(PotDisbursement).filter(
        PotDisbursement.week_id.in_(week_ids)
    ).all() if week_ids else []
    active_disbs = [d for d in disbs if d.status != "voided"]
    total_gross_disbursed = sum(d.gross_amount for d in active_disbs)
    total_net_disbursed = sum(d.net_amount for d in active_disbs)
    total_service_fee = sum(d.service_fee or 0 for d in active_disbs)
    total_voucher = sum(d.voucher_deduction or 0 for d in active_disbs)
    voided_count = sum(1 for d in disbs if d.status == "voided")

    # Association fund
    from routers.draws import _actual_assoc_collected
    association_fund = _actual_assoc_collected(db, [w.id for w in drawn_weeks], cycle_id)
    expenses = db.query(AssociationExpense).filter(AssociationExpense.cycle_id == cycle_id).all()
    expenses_total = sum(e.amount for e in expenses)
    association_balance = association_fund - expenses_total

    # Vendor payments
    vendor_data = _calc_vendor_payments(db, active_disbs, cycle_id)
    total_vendor = sum(v["vendor_payment"] for v in vendor_data.values())
    voucher_retained = total_voucher - total_vendor

    # Distribution cheques issued
    dist_cheques = db.query(DistributionCheque).filter(
        DistributionCheque.cycle_id == cycle_id
    ).all()
    total_distributed = sum(c.amount for c in dist_cheques)
    distributed_collected = sum(c.amount for c in dist_cheques if c.status == "collected")

    # Cash position
    cash_out = total_net_disbursed + total_vendor + expenses_total + total_distributed
    cash_balance = total_collected - cash_out

    return {
        "cycle": {
            "id": cycle.id,
            "name": cycle.name,
            "start_date": cycle.start_date.isoformat(),
            "status": cycle.status,
            "draw_phase": cycle.draw_phase,
        },
        "totals": {
            "total_weeks": total_weeks,
            "drawn_weeks": len(drawn_weeks),
            "total_members": total_members,
            "full_spots": full_spots,
            "half_spots": half_spots,
        },
        "collections": {
            "total_collected": round(total_collected, 2),
            "total_missed": round(total_missed, 2),
            "collection_rate": round(total_collected / (total_collected + total_missed) * 100, 1)
                               if (total_collected + total_missed) else 0,
        },
        "disbursements": {
            "count": len(active_disbs),
            "voided_count": voided_count,
            "total_gross": round(total_gross_disbursed, 2),
            "total_service_fee": round(total_service_fee, 2),
            "total_voucher": round(total_voucher, 2),
            "total_net": round(total_net_disbursed, 2),
        },
        "association": {
            "fund_collected": round(association_fund, 2),
            "expenses": round(expenses_total, 2),
            "balance": round(association_balance, 2),
            "voucher_retained": round(voucher_retained, 2),
            "vendor_paid": round(total_vendor, 2),
        },
        "distribution": {
            "total_issued": round(total_distributed, 2),
            "total_collected": round(distributed_collected, 2),
            "cheques_count": len(dist_cheques),
        },
        "cash_balance": round(cash_balance, 2),
        "settings": {
            "full_spot_amount": cfg.full_spot_amount,
            "half_spot_amount": cfg.half_spot_amount,
            "association_deduction": cfg.association_deduction,
            "full_spot_voucher": cfg.full_spot_voucher,
            "half_spot_voucher": cfg.half_spot_voucher,
        },
    }


# ── Audit Log ─────────────────────────────────────────────────────────────────

@router.get("/audit-log")
def audit_log(request: Request, limit: int = 100, offset: int = 0,
              table_name: Optional[str] = None, db: Session = Depends(get_db)):
    _require_feature(request, db, "view_reports")
    from database import AuditLog
    q = db.query(AuditLog).order_by(AuditLog.timestamp.desc())
    if table_name:
        q = q.filter(AuditLog.table_name == table_name)
    total = q.count()
    rows = q.offset(offset).limit(limit).all()
    return {
        "total": total,
        "rows": [
            {
                "id": r.id,
                "timestamp": r.timestamp.isoformat(),
                "username": r.username,
                "action": r.action,
                "table_name": r.table_name,
                "record_id": r.record_id,
                "description": r.description,
                "old_value": r.old_value,
                "new_value": r.new_value,
            }
            for r in rows
        ],
    }

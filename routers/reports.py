from fastapi import APIRouter, Depends, HTTPException
from typing import Optional
from sqlalchemy.orm import Session
from database import get_db, Member, MemberSpot, Week, Payment, PotTransaction, Spot, Cycle, Settings

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

    total_weeks = db.query(Week).filter(Week.cycle_id == cycle.id).count() if cycle else 0

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
        "cycle": {
            "id": cycle.id,
            "name": cycle.name,
            "start_date": cycle.start_date.isoformat(),
        } if cycle else None,
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

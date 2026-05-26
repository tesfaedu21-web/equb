from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db, Member, MemberSpot, Spot, Payment, Week, Cycle, PotTransaction, Settings

router = APIRouter()


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _normalize_phone(phone: str) -> list[str]:
    """Return candidate normalised forms of the phone number."""
    p = phone.strip().replace(" ", "").replace("-", "")
    variants = {p}
    if p.startswith("+251"):
        variants.add("0" + p[4:])
        variants.add(p[1:])
    elif p.startswith("251") and len(p) == 12:
        variants.add("+" + p)
        variants.add("0" + p[3:])
    elif p.startswith("0") and len(p) == 10:
        variants.add("+251" + p[1:])
        variants.add("251" + p[1:])
    return list(variants)


@router.get("/lookup")
def portal_lookup(phone: str, spot_number: int, db: Session = Depends(get_db)):
    """
    Public endpoint — returns read-only member data.
    Requires phone number + spot number for identification.
    No auth required; only non-sensitive data is exposed.
    """
    if not phone or len(phone.strip()) < 7:
        raise HTTPException(400, "Phone number too short")

    # Join Member → MemberSpot → Spot in one query so that:
    # - a member with multiple spots can log in with any of their spot numbers
    # - two different members sharing the same phone but different spots are
    #   each resolved correctly (no .first() on phone alone)
    active_cycle = db.query(Cycle).filter(Cycle.status == "active").first()
    cycle_id = active_cycle.id if active_cycle else None

    variants = _normalize_phone(phone.strip())
    sa_found = None
    for v in variants:
        q = (
            db.query(MemberSpot)
            .join(MemberSpot.member)
            .join(MemberSpot.spot)
            .filter(
                Member.phone == v,
                Spot.number == spot_number,
                MemberSpot.is_active == True,
            )
        )
        if cycle_id:
            q = q.filter(MemberSpot.cycle_id == cycle_id)
        sa_found = q.first()
        if sa_found:
            break

    if not sa_found:
        raise HTTPException(404, "No member found with this phone number and spot number")

    member = sa_found.member
    if member.status == "left":
        raise HTTPException(404, "No member found with this phone number and spot number")

    sas = [sa for sa in member.spot_assignments
           if sa.is_active and (cycle_id is None or sa.cycle_id == cycle_id)]
    spots = [{"number": sa.spot.number, "share": sa.share,
              "spot_type": sa.spot.spot_type if sa.spot else "member"}
             for sa in sas if sa.spot]

    # Payment history in active cycle
    today = _utcnow().date()
    payments = []
    if cycle_id:
        week_ids = [r[0] for r in db.query(Week.id).filter(Week.cycle_id == cycle_id).all()]
        if week_ids:
            ps = (db.query(Payment)
                  .filter(Payment.member_id == member.id,
                          Payment.week_id.in_(week_ids))
                  .join(Week)
                  .order_by(Week.week_number)
                  .all())
            for p in ps:
                w = p.week
                if not w:
                    continue
                payments.append({
                    "id": p.id,
                    "week_number": w.week_number,
                    "draw_date": w.draw_date.isoformat(),
                    "amount": float(p.amount),
                    "status": p.status,
                    "paid_date": p.paid_date.isoformat() if p.paid_date else None,
                    "payment_method": p.payment_method,
                    "is_past": w.draw_date.date() <= today,
                })

    # Upcoming weeks (pending, not yet drawn)
    upcoming = []
    if cycle_id:
        up_weeks = (db.query(Week)
                    .filter(Week.cycle_id == cycle_id,
                            Week.status == "pending",
                            Week.draw_date > datetime.combine(today, datetime.min.time()))
                    .order_by(Week.draw_date)
                    .limit(5)
                    .all())
        for w in up_weeks:
            upcoming.append({
                "week_number": w.week_number,
                "draw_date": w.draw_date.isoformat(),
                "is_group_week": bool(w.is_group_week),
            })

    # Pot wins
    wins = []
    if cycle_id and week_ids:
        txs = db.query(PotTransaction).filter(
            PotTransaction.week_id.in_(week_ids),
            PotTransaction.buyer_id == member.id,
        ).all()
        member_spot_ids = [sa.spot_id for sa in sas if sa.spot_id]
        direct_win_weeks = (db.query(Week)
                            .filter(Week.id.in_(week_ids),
                                    Week.winner_spot_id.in_(member_spot_ids),
                                    Week.status.in_(["drawn", "sold"]))
                            .all()) if member_spot_ids else []
        for w in direct_win_weeks:
            tx = next((t for t in txs if t.week_id == w.id), None)
            wins.append({
                "week_number": w.week_number,
                "draw_date": w.draw_date.isoformat(),
                "buyer_receives": float(tx.buyer_receives) if tx else float(w.net_pot or 0),
                "net_pot": float(w.net_pot or 0),
            })

    # Summary stats
    paid_count = sum(1 for p in payments if p["status"] == "paid")
    missed_count = sum(1 for p in payments if p["status"] in ("missed",))
    total_paid_amount = sum(p["amount"] for p in payments if p["status"] == "paid")
    outstanding = [p for p in payments if p["status"] in ("pending", "late", "missed") and p["is_past"]]

    # Next payment due: earliest overdue first, then next upcoming unpaid
    next_due = None
    overdue_payments = sorted(
        [p for p in payments if p["status"] in ("late", "missed") and p["is_past"]],
        key=lambda x: x["draw_date"],
    )
    if overdue_payments:
        next_due = overdue_payments[0]
    else:
        upcoming_unpaid = sorted(
            [p for p in payments if p["status"] == "pending" and not p["is_past"]],
            key=lambda x: x["draw_date"],
        )
        if upcoming_unpaid:
            next_due = upcoming_unpaid[0]

    # Scheduled pot week: a future week assigned to this member's spot (not yet drawn)
    pot_week = None
    member_spot_ids = [sa.spot_id for sa in sas if sa.spot_id]
    if member_spot_ids and cycle_id:
        pw = (db.query(Week)
              .filter(Week.cycle_id == cycle_id,
                      Week.winner_spot_id.in_(member_spot_ids),
                      Week.status == "pending")
              .order_by(Week.draw_date)
              .first())
        if pw:
            pot_week = {
                "week_number": pw.week_number,
                "draw_date": pw.draw_date.isoformat(),
                "net_pot": float(pw.net_pot or 0),
            }

    # Admin contact phone from group settings
    gs = db.query(Settings).first()
    admin_contact = getattr(gs, "admin_phone", None) if gs else None

    return {
        "member_id": member.id,
        "member_name": member.name,
        "status": member.status,
        "spots": spots,
        "cycle_name": active_cycle.name if active_cycle else None,
        "payments": payments,
        "upcoming": upcoming,
        "wins": wins,
        "next_due": next_due,
        "pot_week": pot_week,
        "admin_contact": admin_contact,
        "summary": {
            "total_weeks": len(payments),
            "paid_weeks": paid_count,
            "missed_weeks": missed_count,
            "total_paid_amount": total_paid_amount,
            "outstanding_count": len(outstanding),
            "outstanding_amount": sum(p["amount"] for p in outstanding),
        },
    }

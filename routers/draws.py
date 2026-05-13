from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timedelta
from database import (get_db, Week, Cycle, Spot, Member, MemberSpot,
                      PotTransaction, Settings, Payment, PaymentBatch,
                      PotDisbursement, AssociationExpense)

router = APIRouter()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _check_fully_paid(member: Member, up_to_week_number: int, db: Session) -> dict:
    unpaid = (db.query(Payment).join(Week)
              .filter(Payment.member_id == member.id,
                      Payment.status.in_(["pending", "late", "missed"]),
                      Week.week_number <= up_to_week_number).all())
    return {
        "fully_paid": len(unpaid) == 0,
        "unpaid_count": len(unpaid),
        "unpaid_amount": sum(p.amount for p in unpaid),
        "unpaid_weeks": sorted(p.week.week_number for p in unpaid),
    }


def _calculate_pot(db: Session, cycle_id: Optional[int] = None):
    """
    Calculate gross/assoc/net pot.

    When cycle_id is given, uses actual member assignments for that cycle:
      - full assignment  → full_spot_amount gross, association_deduction (1000) each
      - half assignment  → half_spot_amount gross, association_deduction/2 (500) each
      - association spots → full_spot_amount gross, no association deduction (group-owned)

    When cycle_id is None (cycle not yet created), falls back to the theoretical
    all-spots × full_spot_amount calculation.
    """
    settings = db.query(Settings).first()

    if cycle_id:
        assignments = db.query(MemberSpot).filter(
            MemberSpot.cycle_id == cycle_id,
            MemberSpot.is_active == True,
        ).all()
        full_count = sum(1 for a in assignments if a.share == "full")
        half_count = sum(1 for a in assignments if a.share == "half")
        assoc_spot_count = db.query(Spot).filter(Spot.spot_type == "association").count()

        gross = (full_count * settings.full_spot_amount
                 + half_count * settings.half_spot_amount
                 + assoc_spot_count * settings.full_spot_amount)
        # Full assignment: 1000/week; half assignment: 500/week (deduction per spot always 1000)
        assoc = (full_count * settings.association_deduction
                 + half_count * (settings.association_deduction / 2))
        net = gross - assoc
    else:
        # Theoretical: all spots × full rate (used at cycle-creation time before members join)
        total_spots = db.query(Spot).filter(Spot.status == "active").count()
        member_spots = db.query(Spot).filter(
            Spot.status == "active", Spot.spot_type == "member"
        ).count()
        gross = total_spots * settings.full_spot_amount
        assoc = member_spots * settings.association_deduction
        net = gross - assoc

    return gross, assoc, net


def week_to_dict(w: Week) -> dict:
    tx = w.transactions[0] if w.transactions else None
    winner_spot = None
    if w.winner_spot:
        members = [
            {"id": sa.member.id, "name": sa.member.name, "share": sa.share}
            for sa in w.winner_spot.spot_assignments if sa.is_active
        ]
        winner_spot = {"id": w.winner_spot.id, "number": w.winner_spot.number, "members": members}
    transaction = None
    if tx:
        transaction = {
            "id": tx.id,
            "type": tx.transaction_type,
            "buyer": {"id": tx.buyer_id, "name": tx.buyer.name if tx.buyer else None},
            "seller": {"id": tx.seller_id, "name": tx.seller.name if tx.seller else None} if tx.seller_id else None,
            "percentage": tx.percentage,
            "gross_amount": tx.gross_amount,
            "seller_fee": tx.seller_fee,
            "buyer_receives": tx.buyer_receives,
        }
    return {
        "id": w.id,
        "cycle_id": w.cycle_id,
        "week_number": w.week_number,
        "draw_date": w.draw_date.isoformat(),
        "is_group_week": w.is_group_week,
        "is_worker_week": bool(getattr(w, "is_worker_week", False)),
        "gross_pot": w.gross_pot,
        "association_amount": w.association_amount,
        "net_pot": w.net_pot,
        "status": w.status,
        "winner_spot": winner_spot,
        "transaction": transaction,
        "notes": w.notes,
    }


# ── Pydantic models ───────────────────────────────────────────────────────────

class CycleCreate(BaseModel):
    name: str
    start_date: str
    notes: Optional[str] = None
    # Optional: spot counts for this cycle (overrides settings defaults)
    total_member_spots: Optional[int] = None
    total_assoc_spots: Optional[int] = None
    # Optional: override global settings for this new cycle
    full_spot_amount: Optional[float] = None
    half_spot_amount: Optional[float] = None
    association_deduction: Optional[float] = None
    full_spot_voucher: Optional[float] = None
    half_spot_voucher: Optional[float] = None


class DrawResult(BaseModel):
    winner_spot_id: int


class BatchDrawResult(BaseModel):
    """Draw results for multiple weeks at once (batch draw event)."""
    draws: List[dict]   # [{week_id, winner_spot_id}]


class PotSale(BaseModel):
    transaction_type: str
    original_winner_id: Optional[int] = None
    seller_id: Optional[int] = None
    buyer_id: int
    percentage: Optional[float] = None
    notes: Optional[str] = None


class ExpenseCreate(BaseModel):
    cycle_id: int
    description: str
    amount: float
    expense_date: Optional[str] = None
    notes: Optional[str] = None


# ── Cycles ────────────────────────────────────────────────────────────────────

@router.get("/cycles")
def list_cycles(db: Session = Depends(get_db)):
    cycles = db.query(Cycle).order_by(Cycle.id.desc()).all()
    return [
        {
            "id": c.id, "name": c.name,
            "start_date": c.start_date.isoformat(),
            "end_date": c.end_date.isoformat() if c.end_date else None,
            "status": c.status,
            "draw_phase": c.draw_phase,
            "draw_start_week": c.draw_start_week,
            "total_weeks": len(c.weeks),
            "drawn_weeks": sum(1 for w in c.weeks if w.status in ("drawn", "sold")),
            "notes": c.notes,
        }
        for c in cycles
    ]


@router.post("/cycles")
def create_cycle(data: CycleCreate, request: Request, db: Session = Depends(get_db)):
    if getattr(request.state, "user_role", None) != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    start = datetime.fromisoformat(data.start_date)
    settings = db.query(Settings).first()
    if not settings:
        raise HTTPException(status_code=500, detail="Settings not configured")

    # Apply any setting overrides supplied by the caller
    if data.full_spot_amount is not None:
        settings.full_spot_amount = data.full_spot_amount
    if data.half_spot_amount is not None:
        settings.half_spot_amount = data.half_spot_amount
    if data.association_deduction is not None:
        settings.association_deduction = data.association_deduction
    if data.full_spot_voucher is not None:
        settings.full_spot_voucher = data.full_spot_voucher
    if data.half_spot_voucher is not None:
        settings.half_spot_voucher = data.half_spot_voucher

    existing = db.query(Cycle).filter(Cycle.status == "active").first()
    if existing:
        existing.status = "completed"
        existing.end_date = datetime.utcnow()

    # ── Fresh start: reset spot statuses for the new cycle ───────────────────
    # Spots are global slots; their "received" status is reset per cycle
    db.query(Spot).update({"status": "active"}, synchronize_session=False)
    # Members that received their pot go back to active for the new cycle
    db.query(Member).filter(Member.status == "received").update(
        {"status": "active"}, synchronize_session=False
    )
    # NOTE: MemberSpot records are now per-cycle (cycle_id set at assignment time).
    # We do NOT reset or carry over old memberships — each cycle builds its own from scratch.

    # Use caller-supplied spot counts, or fall back to settings
    n_member = data.total_member_spots if data.total_member_spots else settings.total_member_spots
    n_assoc  = data.total_assoc_spots  if data.total_assoc_spots  is not None else settings.total_assoc_spots
    total_spots = n_member + n_assoc

    # Persist the chosen spot counts back to settings for future reference
    settings.total_member_spots = n_member
    settings.total_assoc_spots  = n_assoc

    cycle = Cycle(name=data.name, start_date=start, notes=data.notes, draw_phase="collection")
    db.add(cycle)
    db.flush()

    # Calculate pot using the cycle's own spot counts (not the Spot table size)
    # Theoretical: all spots pay full rate; adjusted per-member once assignments exist
    gross = (n_member + n_assoc) * settings.full_spot_amount
    assoc = n_member * settings.association_deduction
    net   = gross - assoc

    interval = getattr(settings, "group_week_interval", 4)
    for i in range(1, total_spots + 1):
        draw_date = start + timedelta(weeks=i - 1)
        # Snap to Sunday
        days_to_sunday = (6 - draw_date.weekday()) % 7
        if days_to_sunday:
            draw_date = draw_date + timedelta(days=days_to_sunday)
        w = Week(
            cycle_id=cycle.id, week_number=i, draw_date=draw_date,
            is_group_week=(i % interval == 0),
            gross_pot=gross, association_amount=assoc, net_pot=net,
        )
        db.add(w)


    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

    db.refresh(cycle)
    total_weeks = db.query(Week).filter(Week.cycle_id == cycle.id).count()
    return {"id": cycle.id, "name": cycle.name, "total_weeks": total_weeks,
            "draw_phase": "collection"}


@router.post("/cycles/{cycle_id}/start-draws")
def start_draws(cycle_id: int, at_week_number: int, request: Request, db: Session = Depends(get_db)):
    if getattr(request.state, "user_role", None) != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    """Admin manually triggers the start of draws from a specific week number."""
    cycle = db.query(Cycle).filter(Cycle.id == cycle_id).first()
    if not cycle:
        raise HTTPException(status_code=404, detail="Cycle not found")
    if cycle.draw_phase == "active":
        raise HTTPException(status_code=400, detail="Draws already started")
    cycle.draw_phase = "active"
    cycle.draw_start_week = at_week_number
    cycle.draw_started_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "draw_start_week": at_week_number,
            "pending_draws": at_week_number,
            "message": f"Draws started from week {at_week_number}. {at_week_number} batch draws are now available."}


@router.post("/cycles/{cycle_id}/recalculate-pot")
def recalculate_pot(cycle_id: int, request: Request, db: Session = Depends(get_db)):
    """
    Recalculate gross/assoc/net pot for all pending weeks in a cycle using
    the actual current member assignments (full vs half per spot).
    Only pending weeks are updated — drawn/sold weeks are left as recorded.
    Admin only.
    """
    if getattr(request.state, "user_role", None) != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    cycle = db.query(Cycle).filter(Cycle.id == cycle_id).first()
    if not cycle:
        raise HTTPException(status_code=404, detail="Cycle not found")

    gross, assoc, net = _calculate_pot(db, cycle_id=cycle_id)
    pending_weeks = db.query(Week).filter(
        Week.cycle_id == cycle_id, Week.status == "pending"
    ).all()
    for w in pending_weeks:
        w.gross_pot = gross
        w.association_amount = assoc
        w.net_pot = net
    db.commit()
    return {
        "ok": True,
        "updated_weeks": len(pending_weeks),
        "gross_pot": gross,
        "association_amount": assoc,
        "net_pot": net,
    }


@router.get("/cycles/{cycle_id}/weeks")
def list_weeks(cycle_id: int, db: Session = Depends(get_db)):
    weeks = db.query(Week).filter(Week.cycle_id == cycle_id).order_by(Week.week_number).all()
    return [week_to_dict(w) for w in weeks]


# ── Payment checks ────────────────────────────────────────────────────────────

@router.get("/weeks/{week_id}/check-payment/{spot_id}")
def check_winner_payment(week_id: int, spot_id: int, db: Session = Depends(get_db)):
    w = db.query(Week).filter(Week.id == week_id).first()
    if not w:
        raise HTTPException(status_code=404, detail="Week not found")
    spot = db.query(Spot).filter(Spot.id == spot_id).first()
    if not spot:
        raise HTTPException(status_code=404, detail="Spot not found")
    results, all_paid = [], True
    for sa in spot.spot_assignments:
        if not sa.is_active:
            continue
        s = _check_fully_paid(sa.member, w.week_number, db)
        if not s["fully_paid"]:
            all_paid = False
        results.append({"member_id": sa.member.id, "name": sa.member.name, **s})
    return {"spot_id": spot_id, "week_number": w.week_number,
            "all_paid": all_paid, "members": results}


@router.get("/weeks/{week_id}/check-payment-member/{member_id}")
def check_buyer_payment(week_id: int, member_id: int, db: Session = Depends(get_db)):
    w = db.query(Week).filter(Week.id == week_id).first()
    if not w:
        raise HTTPException(status_code=404, detail="Week not found")
    m = db.query(Member).filter(Member.id == member_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="Member not found")
    s = _check_fully_paid(m, w.week_number, db)
    return {"member_id": member_id, "name": m.name, "week_number": w.week_number, **s}


# ── Single draw ───────────────────────────────────────────────────────────────

@router.get("/weeks/{week_id}")
def get_week(week_id: int, db: Session = Depends(get_db)):
    w = db.query(Week).filter(Week.id == week_id).first()
    if not w:
        raise HTTPException(status_code=404, detail="Week not found")
    return week_to_dict(w)


@router.post("/weeks/{week_id}/draw")
def record_draw(week_id: int, data: DrawResult, request: Request, db: Session = Depends(get_db)):
    if getattr(request.state, "user_role", None) != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    w = db.query(Week).filter(Week.id == week_id).first()
    if not w:
        raise HTTPException(status_code=404, detail="Week not found")
    if w.status != "pending":
        raise HTTPException(status_code=400, detail="Week already processed")
    if getattr(w, "is_worker_week", False):
        raise HTTPException(status_code=400, detail="Worker week has no draw — it is a payment collection week for staff")
    if w.is_group_week:
        raise HTTPException(status_code=400, detail="Group week must use the sale endpoint")
    if w.cycle.draw_phase != "active":
        raise HTTPException(status_code=400, detail="Draws have not been started yet by admin")

    spot = db.query(Spot).filter(Spot.id == data.winner_spot_id).first()
    if not spot:
        raise HTTPException(status_code=404, detail="Spot not found")

    # Association spot → must go through sell endpoint (profit tracked)
    if spot.spot_type == "association":
        raise HTTPException(
            status_code=400,
            detail="Association spot must be sold, not drawn directly. Use the sell endpoint."
        )

    # All members of winning spot must be active (not left) and fully paid
    for sa in [sa for sa in spot.spot_assignments if sa.is_active]:
        if sa.member.status == "left":
            raise HTTPException(
                status_code=400,
                detail=f"{sa.member.name} has left the group and cannot receive a pot draw."
            )
        s = _check_fully_paid(sa.member, w.week_number, db)
        if not s["fully_paid"]:
            raise HTTPException(
                status_code=400,
                detail=f"{sa.member.name} has {s['unpaid_count']} unpaid week(s) "
                       f"(weeks {s['unpaid_weeks']}). Pot is on hold until full payment."
            )

    w.winner_spot_id = data.winner_spot_id
    w.status = "drawn"
    spot.status = "received"
    for sa in spot.spot_assignments:
        if sa.is_active:
            sa.member.status = "received"

    db.commit()
    return week_to_dict(w)


# ── Batch draw (when admin starts draws) ─────────────────────────────────────

@router.post("/batch-draw")
def record_batch_draw(data: BatchDrawResult, request: Request, db: Session = Depends(get_db)):
    if getattr(request.state, "user_role", None) != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    """Record multiple draw results at once (batch event after collection phase)."""
    results = []
    for item in data.draws:
        week_id = item.get("week_id")
        winner_spot_id = item.get("winner_spot_id")
        w = db.query(Week).filter(Week.id == week_id).first()
        if not w or w.status != "pending":
            results.append({"week_id": week_id, "status": "skipped",
                             "reason": "not found or already processed"})
            continue
        spot = db.query(Spot).filter(Spot.id == winner_spot_id).first()
        if not spot or spot.spot_type == "association":
            results.append({"week_id": week_id, "status": "skipped",
                             "reason": "invalid spot"})
            continue
        w.winner_spot_id = winner_spot_id
        w.status = "drawn"
        spot.status = "received"
        for sa in spot.spot_assignments:
            if sa.is_active:
                sa.member.status = "received"
        results.append({"week_id": week_id, "week_number": w.week_number,
                        "winner_spot": spot.number, "status": "drawn"})
    db.commit()
    return {"processed": len([r for r in results if r["status"] == "drawn"]),
            "results": results}


# ── Pot sale ──────────────────────────────────────────────────────────────────

@router.post("/weeks/{week_id}/sell")
def record_sale(week_id: int, data: PotSale, request: Request, db: Session = Depends(get_db)):
    if getattr(request.state, "user_role", None) != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    w = db.query(Week).filter(Week.id == week_id).first()
    if not w:
        raise HTTPException(status_code=404, detail="Week not found")
    if w.status not in ("pending", "drawn"):
        raise HTTPException(status_code=400, detail="Week already processed")
    if w.cycle.draw_phase != "active" and data.transaction_type != "assoc_spot_sale":
        raise HTTPException(status_code=400, detail="Draws have not been started yet by admin")

    buyer = db.query(Member).filter(Member.id == data.buyer_id).first()
    if not buyer:
        raise HTTPException(status_code=404, detail="Buyer not found")
    if buyer.status == "left":
        raise HTTPException(status_code=400, detail=f"{buyer.name} has left the group and cannot buy a pot")
    if buyer.status != "active":
        raise HTTPException(status_code=400, detail="Buyer must be an active member (not already received)")

    pay_status = _check_fully_paid(buyer, w.week_number, db)
    if not pay_status["fully_paid"]:
        raise HTTPException(
            status_code=400,
            detail=f"{buyer.name} has {pay_status['unpaid_count']} unpaid week(s) "
                   f"(weeks {pay_status['unpaid_weeks']}). Must be fully paid to buy this pot."
        )

    gross = w.gross_pot or 0
    seller_fee = 0.0
    buyer_receives = w.net_pot or 0

    if data.transaction_type == "member_sale" and data.percentage:
        seller_fee = gross * (data.percentage / 100)
        buyer_receives = (w.net_pot or 0) - seller_fee
    elif data.transaction_type in ("assoc_spot_sale", "group_week_sale") and data.percentage:
        # Profit (seller_fee) goes to association fund
        seller_fee = gross * (data.percentage / 100)
        buyer_receives = (w.net_pot or 0) - seller_fee

    tx = PotTransaction(
        week_id=week_id,
        transaction_type=data.transaction_type,
        original_winner_id=data.original_winner_id,
        seller_id=data.seller_id,
        buyer_id=data.buyer_id,
        percentage=data.percentage,
        gross_amount=gross,
        seller_fee=seller_fee,
        buyer_receives=buyer_receives,
        notes=data.notes,
    )
    db.add(tx)

    # Mark buyer's spot as received
    buyer.status = "received"
    if buyer.spot_assignments:
        for sa in buyer.spot_assignments:
            if sa.is_active:
                sa.spot.status = "received"

    w.status = "sold"
    db.commit()
    return week_to_dict(w)


# ── Active lists for UI ───────────────────────────────────────────────────────

@router.get("/active-spots")
def active_spots(db: Session = Depends(get_db)):
    """Returns only member-type active spots (association spots excluded — use sell endpoint)."""
    spots = (db.query(Spot).filter(Spot.status == "active", Spot.spot_type == "member")
             .order_by(Spot.number).all())
    return [
        {
            "id": s.id, "number": s.number, "type": s.spot_type,
            "members": [
                {"id": sa.member.id, "name": sa.member.name, "share": sa.share}
                for sa in s.spot_assignments if sa.is_active
            ],
            "share": (s.spot_assignments[0].share
                      if any(sa.is_active for sa in s.spot_assignments)
                      else "full"),
        }
        for s in spots
    ]


@router.get("/active-members")
def active_members_for_sale(db: Session = Depends(get_db)):
    members = db.query(Member).filter(Member.status == "active").order_by(Member.name).all()
    return [
        {
            "id": m.id, "name": m.name,
            "spot_numbers": [sa.spot.number for sa in m.spot_assignments if sa.is_active],
            "spot_count": sum(1 for sa in m.spot_assignments if sa.is_active),
        }
        for m in members
    ]


@router.post("/cycles/{cycle_id}/reactivate")
def reactivate_cycle(cycle_id: int, request: Request, db: Session = Depends(get_db)):
    """Restore a completed cycle back to active status. Admin only."""
    if getattr(request.state, "user_role", None) != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    cycle = db.query(Cycle).filter(Cycle.id == cycle_id).first()
    if not cycle:
        raise HTTPException(status_code=404, detail="Cycle not found")
    # Close any currently active cycle first
    other = db.query(Cycle).filter(Cycle.status == "active", Cycle.id != cycle_id).first()
    if other:
        other.status = "completed"
        other.end_date = datetime.utcnow()
    cycle.status = "active"
    cycle.end_date = None
    db.commit()
    return {"ok": True, "id": cycle.id, "name": cycle.name, "status": cycle.status}


@router.delete("/cycles/{cycle_id}")
def delete_cycle(cycle_id: int, request: Request, db: Session = Depends(get_db)):
    """Permanently delete a cycle and all its related data. Admin only."""
    if getattr(request.state, "user_role", None) != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    cycle = db.query(Cycle).filter(Cycle.id == cycle_id).first()
    if not cycle:
        raise HTTPException(status_code=404, detail="Cycle not found")

    week_ids = [w.id for w in cycle.weeks]
    if week_ids:
        # Collect batch IDs before deleting payments
        batch_ids = [r[0] for r in db.query(Payment.batch_id).filter(
            Payment.week_id.in_(week_ids), Payment.batch_id.isnot(None)
        ).distinct().all()]
        db.query(PotDisbursement).filter(
            PotDisbursement.week_id.in_(week_ids)
        ).delete(synchronize_session=False)
        db.query(PotTransaction).filter(
            PotTransaction.week_id.in_(week_ids)
        ).delete(synchronize_session=False)
        db.query(Payment).filter(
            Payment.week_id.in_(week_ids)
        ).delete(synchronize_session=False)
        if batch_ids:
            db.query(PaymentBatch).filter(
                PaymentBatch.id.in_(batch_ids)
            ).delete(synchronize_session=False)
        db.query(Week).filter(Week.cycle_id == cycle_id).delete(synchronize_session=False)

    # Delete association expenses for this cycle
    db.query(AssociationExpense).filter(
        AssociationExpense.cycle_id == cycle_id
    ).delete(synchronize_session=False)

    # Find members who belong to this cycle
    cycle_member_ids = [r[0] for r in db.query(MemberSpot.member_id).filter(
        MemberSpot.cycle_id == cycle_id
    ).distinct().all()]

    # Delete member-spot assignments for this cycle
    db.query(MemberSpot).filter(
        MemberSpot.cycle_id == cycle_id
    ).delete(synchronize_session=False)

    db.flush()  # flush so the MemberSpot deletions are visible for the next query

    # Delete Member records that have no remaining spot assignments in any other cycle
    if cycle_member_ids:
        still_assigned = {r[0] for r in db.query(MemberSpot.member_id).filter(
            MemberSpot.member_id.in_(cycle_member_ids)
        ).all()}
        orphan_ids = [mid for mid in cycle_member_ids if mid not in still_assigned]
        if orphan_ids:
            from database import Member as _Member, NotificationLog as _NLog
            db.query(_NLog).filter(_NLog.member_id.in_(orphan_ids)).delete(synchronize_session=False)
            db.query(_Member).filter(_Member.id.in_(orphan_ids)).delete(synchronize_session=False)

    cycle_name = cycle.name
    db.flush()
    from database import Cycle as _Cycle
    db.query(_Cycle).filter(_Cycle.id == cycle_id).delete(synchronize_session=False)
    db.commit()
    return {"ok": True, "deleted": cycle_name}


@router.get("/association-fund")
def association_fund(cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    """
    Full association fund summary for a cycle:
      weekly_deductions   = sum of Week.association_amount for drawn/sold weeks
      spot_sales_profit   = sum of seller_fee for assoc_spot_sale transactions
      total_fund          = weekly_deductions + spot_sales_profit
      total_expenses      = sum of AssociationExpense.amount for this cycle
      net_fund            = total_fund - total_expenses
      per_full_return     = net_fund / total_spot_shares (1 share per full, 0.5 per half)
      per_half_return     = per_full_return / 2
    """
    if not cycle_id:
        active = db.query(Cycle).filter(Cycle.status == "active").first()
        cycle_id = active.id if active else None

    cycle = db.query(Cycle).filter(Cycle.id == cycle_id).first() if cycle_id else None

    # ── Weekly deductions (from drawn/sold weeks) ─────────────────────────────
    weeks_q = db.query(Week).filter(Week.cycle_id == cycle_id) if cycle_id else db.query(Week)
    completed_weeks = weeks_q.filter(Week.status.in_(["drawn", "sold"])).all()
    weekly_deductions = sum(w.association_amount or 0 for w in completed_weeks)

    # ── Association spot + group-week sales profit ────────────────────────────
    tx_q = (db.query(PotTransaction)
            .filter(PotTransaction.transaction_type.in_(["assoc_spot_sale", "group_week_sale"])))
    if cycle_id:
        week_ids = [w.id for w in weeks_q.all()]
        if week_ids:
            tx_q = tx_q.filter(PotTransaction.week_id.in_(week_ids))
        else:
            tx_q = tx_q.filter(False)
    assoc_txs = tx_q.all()
    spot_sales_profit = sum(t.seller_fee or 0 for t in assoc_txs)

    total_fund = weekly_deductions + spot_sales_profit

    # ── Expenses ──────────────────────────────────────────────────────────────
    exp_q = db.query(AssociationExpense).filter(AssociationExpense.cycle_id == cycle_id) \
        if cycle_id else db.query(AssociationExpense)
    expenses = exp_q.order_by(AssociationExpense.expense_date).all()
    total_expenses = sum(e.amount for e in expenses)
    net_fund = total_fund - total_expenses

    # ── Per-member return calculation ─────────────────────────────────────────
    # Each full assignment = 1 share; each half assignment = 0.5 share
    assignments = []
    if cycle_id:
        assignments = db.query(MemberSpot).filter(
            MemberSpot.cycle_id == cycle_id,
            MemberSpot.is_active == True,
        ).all()
    full_assignments = sum(1 for a in assignments if a.share == "full")
    half_assignments = sum(1 for a in assignments if a.share == "half")
    total_shares = full_assignments * 1.0 + half_assignments * 0.5

    per_full_return = round(net_fund / total_shares, 2) if total_shares > 0 else 0
    per_half_return = round(per_full_return / 2, 2)

    return {
        "cycle_id": cycle_id,
        "cycle_name": cycle.name if cycle else None,
        "weeks_completed": len(completed_weeks),
        "weekly_deductions": weekly_deductions,
        "spot_sales_profit": spot_sales_profit,
        "total_fund": total_fund,
        "total_expenses": total_expenses,
        "net_fund": net_fund,
        "member_assignments": {
            "full": full_assignments,
            "half": half_assignments,
            "total_shares": total_shares,
        },
        "per_full_member_return": per_full_return,
        "per_half_member_return": per_half_return,
        "expenses": [
            {"id": e.id, "description": e.description, "amount": e.amount,
             "expense_date": e.expense_date.isoformat(), "notes": e.notes}
            for e in expenses
        ],
        "spot_sale_transactions": [
            {
                "week_number": t.week.week_number if t.week else None,
                "buyer": t.buyer.name if t.buyer else None,
                "percentage": t.percentage,
                "profit": t.seller_fee,
                "buyer_receives": t.buyer_receives,
                "date": t.transaction_date.isoformat(),
            }
            for t in assoc_txs
        ],
    }


# ── Association Expenses ──────────────────────────────────────────────────────

@router.post("/association-expenses")
def add_expense(data: ExpenseCreate, request: Request, db: Session = Depends(get_db)):
    """Record an expense deducted from the association fund (paper, pen, etc.)."""
    if getattr(request.state, "user_role", None) != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    cycle = db.query(Cycle).filter(Cycle.id == data.cycle_id).first()
    if not cycle:
        raise HTTPException(status_code=404, detail="Cycle not found")
    expense_date = datetime.fromisoformat(data.expense_date) if data.expense_date else datetime.utcnow()
    e = AssociationExpense(
        cycle_id=data.cycle_id,
        description=data.description,
        amount=data.amount,
        expense_date=expense_date,
        notes=data.notes,
    )
    db.add(e)
    db.commit()
    db.refresh(e)
    return {"id": e.id, "description": e.description, "amount": e.amount,
            "expense_date": e.expense_date.isoformat(), "notes": e.notes}


@router.get("/association-expenses")
def list_expenses(cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    q = db.query(AssociationExpense)
    if cycle_id:
        q = q.filter(AssociationExpense.cycle_id == cycle_id)
    return [
        {"id": e.id, "cycle_id": e.cycle_id, "description": e.description,
         "amount": e.amount, "expense_date": e.expense_date.isoformat(), "notes": e.notes}
        for e in q.order_by(AssociationExpense.expense_date).all()
    ]


@router.delete("/association-expenses/{expense_id}")
def delete_expense(expense_id: int, request: Request, db: Session = Depends(get_db)):
    if getattr(request.state, "user_role", None) != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    e = db.query(AssociationExpense).filter(AssociationExpense.id == expense_id).first()
    if not e:
        raise HTTPException(status_code=404, detail="Expense not found")
    db.delete(e)
    db.commit()
    return {"ok": True}


# ── End-of-cycle settlement: return to members ────────────────────────────────

@router.get("/association-settlement/{cycle_id}")
def association_settlement(cycle_id: int, db: Session = Depends(get_db)):
    """
    Per-member breakdown of association fund return at end of cycle.
    Shows how much each member receives back based on their share type and spot count.

    Return is proportional to contribution:
      full spot member:  1 share  → per_full_return ETB
      half spot member:  0.5 share → per_half_return ETB
    A member with multiple spots receives the sum of each spot's return.
    """
    cycle = db.query(Cycle).filter(Cycle.id == cycle_id).first()
    if not cycle:
        raise HTTPException(status_code=404, detail="Cycle not found")

    # Get fund totals (reuse the association_fund logic)
    weeks = db.query(Week).filter(Week.cycle_id == cycle_id).all()
    week_ids = [w.id for w in weeks]
    weekly_deductions = sum(w.association_amount or 0 for w in weeks if w.status in ("drawn", "sold"))

    assoc_txs = db.query(PotTransaction).filter(
        PotTransaction.transaction_type.in_(["assoc_spot_sale", "group_week_sale"]),
        PotTransaction.week_id.in_(week_ids),
    ).all() if week_ids else []
    spot_sales_profit = sum(t.seller_fee or 0 for t in assoc_txs)

    total_expenses = sum(
        e.amount for e in db.query(AssociationExpense).filter(
            AssociationExpense.cycle_id == cycle_id
        ).all()
    )

    total_fund = weekly_deductions + spot_sales_profit
    net_fund = total_fund - total_expenses

    # Assignments for this cycle
    assignments = db.query(MemberSpot).filter(
        MemberSpot.cycle_id == cycle_id,
        MemberSpot.is_active == True,
    ).all()
    full_count = sum(1 for a in assignments if a.share == "full")
    half_count = sum(1 for a in assignments if a.share == "half")
    total_shares = full_count * 1.0 + half_count * 0.5
    per_share = round(net_fund / total_shares, 2) if total_shares > 0 else 0

    # Group assignments by member
    from collections import defaultdict
    member_assignments: dict = defaultdict(list)
    for a in assignments:
        member_assignments[a.member_id].append(a)

    member_returns = []
    for member_id, member_spots in member_assignments.items():
        member = member_spots[0].member
        shares = sum(1.0 if a.share == "full" else 0.5 for a in member_spots)
        return_amount = round(per_share * shares, 2)
        spots_info = [
            {"spot_number": a.spot.number if a.spot else None,
             "share": a.share,
             "contribution_per_week": a.weekly_contribution,
             "association_per_week": a.weekly_contribution * (1000 / 21000) if a.share == "full"
                                     else a.weekly_contribution * (500 / 10500)}
            for a in member_spots
        ]
        member_returns.append({
            "member_id": member_id,
            "member_name": member.name if member else None,
            "phone": member.phone if member else None,
            "spots": spots_info,
            "total_shares": shares,
            "return_amount": return_amount,
        })

    member_returns.sort(key=lambda x: x["member_name"] or "")

    return {
        "cycle_id": cycle_id,
        "cycle_name": cycle.name,
        "weeks_completed": sum(1 for w in weeks if w.status in ("drawn", "sold")),
        "total_weeks": len(weeks),
        "weekly_deductions": weekly_deductions,
        "spot_sales_profit": spot_sales_profit,
        "total_fund": total_fund,
        "total_expenses": total_expenses,
        "net_fund": net_fund,
        "per_share_return": per_share,
        "per_full_member_return": per_share,
        "per_half_member_return": round(per_share / 2, 2),
        "total_members": len(member_returns),
        "members": member_returns,
    }

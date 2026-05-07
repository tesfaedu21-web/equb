from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timedelta
from database import (get_db, Week, Cycle, Spot, Member, MemberSpot,
                      PotTransaction, Settings, Payment, PaymentBatch,
                      PotDisbursement)

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


def _calculate_pot(db: Session):
    settings = db.query(Settings).first()
    total_spots = db.query(Spot).filter(Spot.status == "active").count()
    # Association deduction only from member spots, not association-owned spots
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
            {"id": sa.member.id, "name": sa.member.name}
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

    cycle = Cycle(name=data.name, start_date=start, notes=data.notes, draw_phase="collection")
    db.add(cycle)
    db.flush()

    # Calculate pot amounts ONCE — same for all weeks in this cycle
    gross, assoc, net = _calculate_pot(db)

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

    # Optional extra worker/staff payment week at end of cycle
    if getattr(settings, "include_worker_slot", True):
        worker_week_num = total_spots + 1
        draw_date = start + timedelta(weeks=total_spots)
        days_to_sunday = (6 - draw_date.weekday()) % 7
        if days_to_sunday:
            draw_date = draw_date + timedelta(days=days_to_sunday)
        db.add(Week(
            cycle_id=cycle.id, week_number=worker_week_num, draw_date=draw_date,
            is_group_week=False, is_worker_week=True,
            gross_pot=gross, association_amount=assoc, net_pot=net,
        ))

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

    # Rule 6: all members of winning spot must be fully paid
    for sa in [sa for sa in spot.spot_assignments if sa.is_active]:
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
    if w.status != "pending":
        raise HTTPException(status_code=400, detail="Week already processed")
    if w.cycle.draw_phase != "active" and data.transaction_type != "assoc_spot_sale":
        raise HTTPException(status_code=400, detail="Draws have not been started yet by admin")

    buyer = db.query(Member).filter(Member.id == data.buyer_id).first()
    if not buyer:
        raise HTTPException(status_code=404, detail="Buyer not found")
    if buyer.status != "active":
        raise HTTPException(status_code=400, detail="Buyer is not an active member")

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
    elif data.transaction_type == "assoc_spot_sale" and data.percentage:
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
    spots = (db.query(Spot).filter(Spot.status == "active", Spot.spot_type == "member")
             .order_by(Spot.number).all())
    return [
        {
            "id": s.id, "number": s.number, "type": s.spot_type,
            "members": [
                {"id": sa.member.id, "name": sa.member.name}
                for sa in s.spot_assignments if sa.is_active
            ],
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

    cycle_name = cycle.name
    db.flush()          # push bulk deletes to DB before removing parent
    db.expire(cycle)    # clear stale relationship cache on the cycle object
    db.delete(cycle)
    db.commit()
    return {"ok": True, "deleted": cycle_name}


@router.get("/association-fund")
def association_fund(db: Session = Depends(get_db)):
    """Total profit from association spot sales."""
    assoc_txs = (db.query(PotTransaction)
                 .filter(PotTransaction.transaction_type == "assoc_spot_sale").all())
    total_profit = sum(t.seller_fee or 0 for t in assoc_txs)
    return {
        "total_profit": total_profit,
        "transactions": [
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

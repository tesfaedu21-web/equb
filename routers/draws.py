from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timedelta, timezone
from database import (get_db, Week, Cycle, Spot, Member, MemberSpot,
                      PotTransaction, Settings, Payment, PaymentBatch,
                      PotDisbursement, AssociationExpense, cycle_cfg)
from routers.deps import _require_admin


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)

router = APIRouter()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _actual_assoc_collected(db: Session, completed_week_ids: list, cycle_id: int) -> float:
    """
    Compute the association fund actually collected from paid member payments.
    Full-spot member who paid → contributes association_deduction per week.
    Half-spot member who paid → contributes association_deduction / 2 per week.
    Only counts weeks they actually paid (not missed/pending).
    """
    if not completed_week_ids:
        return 0.0
    cycle   = db.query(Cycle).filter(Cycle.id == cycle_id).first()
    gs      = db.query(Settings).first()
    cfg     = cycle_cfg(cycle, gs)
    assoc_ded = cfg.association_deduction

    paid_payments = db.query(Payment).filter(
        Payment.week_id.in_(completed_week_ids),
        Payment.status == "paid",
    ).all()

    # Build member → list of MemberSpot for this cycle
    ms_by_member: dict = {}
    for ms in db.query(MemberSpot).filter(
        MemberSpot.cycle_id == cycle_id, MemberSpot.is_active == True
    ).all():
        ms_by_member.setdefault(ms.member_id, []).append(ms)

    total = 0.0
    for p in paid_payments:
        for ms in ms_by_member.get(p.member_id, []):
            total += assoc_ded if ms.share == "full" else assoc_ded / 2
    return total


def _check_fully_paid(member: Member, up_to_week_number: int, db: Session,
                       cycle_id: Optional[int] = None) -> dict:
    q = (db.query(Payment).join(Week)
         .filter(Payment.member_id == member.id,
                 Payment.status.in_(["pending", "late", "missed"]),
                 Week.week_number <= up_to_week_number))
    if cycle_id:
        q = q.filter(Week.cycle_id == cycle_id)
    unpaid = q.all()
    return {
        "fully_paid": len(unpaid) == 0,
        "unpaid_count": len(unpaid),
        "unpaid_amount": sum(p.amount for p in unpaid),
        "unpaid_weeks": sorted(p.week.week_number for p in unpaid),
    }


def _calculate_pot(db: Session, cycle_id: Optional[int] = None):
    """
    Calculate gross/assoc/net pot.

    Formula: Gross = member contributions only (full × full_amount + half × half_amount).
    Association spots do NOT add to gross — their draw is funded by the association fund.

    Gross  = full_count × full_amount + half_count × half_amount
    Assoc  = full_count × assoc_ded  + half_count × (assoc_ded / 2)
    Net    = Gross − Assoc
    """
    gs = db.query(Settings).first()

    if cycle_id:
        cycle = db.query(Cycle).filter(Cycle.id == cycle_id).first()
        cfg   = cycle_cfg(cycle, gs)
        assignments = db.query(MemberSpot).filter(
            MemberSpot.cycle_id == cycle_id,
            MemberSpot.is_active == True,
        ).all()
        full_count = sum(1 for a in assignments if a.share == "full")
        half_count = sum(1 for a in assignments if a.share == "half")

        gross = (full_count * cfg.full_spot_amount
                 + half_count * cfg.half_spot_amount)
        assoc = (full_count * cfg.association_deduction
                 + half_count * (cfg.association_deduction / 2))
        net = gross - assoc
    else:
        cfg = cycle_cfg(None, gs)
        member_spots = db.query(Spot).filter(
            Spot.status == "active", Spot.spot_type == "member"
        ).count()
        gross = member_spots * cfg.full_spot_amount
        assoc = member_spots * cfg.association_deduction
        net   = gross - assoc

    return gross, assoc, net


def week_to_dict(w: Week, cfg=None) -> dict:
    tx = w.transactions[0] if w.transactions else None
    winner_spot = None
    if w.winner_spot:
        members = [
            {"id": sa.member.id, "name": sa.member.name, "share": sa.share}
            for sa in w.winner_spot.spot_assignments
            if sa.is_active and sa.cycle_id == w.cycle_id
        ]
        winner_spot = {"id": w.winner_spot.id, "number": w.winner_spot.number,
                       "spot_type": w.winner_spot.spot_type, "members": members}
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
    assoc_contribution = (
        (cfg.total_assoc_spots or 0) * (cfg.full_spot_amount or 0) if cfg else 0
    )
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
        "assoc_contribution": assoc_contribution,
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
    # Association spots are decided when draws start, not at cycle creation
    total_assoc_spots: Optional[int] = None   # kept for API compat; ignored at creation
    # Optional: override global settings for this new cycle
    full_spot_amount: Optional[float] = None
    half_spot_amount: Optional[float] = None
    association_deduction: Optional[float] = None
    full_spot_voucher: Optional[float] = None
    half_spot_voucher: Optional[float] = None


class StartDrawsData(BaseModel):
    at_week_number: int
    total_assoc_spots: int = 0


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
    gs = db.query(Settings).first()
    def _cycle_dict(c):
        cfg = cycle_cfg(c, gs)
        return {
            "id": c.id, "name": c.name,
            "start_date": c.start_date.isoformat(),
            "end_date": c.end_date.isoformat() if c.end_date else None,
            "status": c.status,
            "draw_phase": c.draw_phase,
            "draw_start_week": c.draw_start_week,
            "total_weeks": len(c.weeks),
            "drawn_weeks": sum(1 for w in c.weeks if w.status in ("drawn", "sold")),
            "notes": c.notes,
            "settings": {
                "full_spot_amount": cfg.full_spot_amount,
                "half_spot_amount": cfg.half_spot_amount,
                "association_deduction": cfg.association_deduction,
                "full_spot_voucher": cfg.full_spot_voucher,
                "half_spot_voucher": cfg.half_spot_voucher,
                "total_member_spots": cfg.total_member_spots,
                "total_assoc_spots": cfg.total_assoc_spots,
            },
        }
    return [_cycle_dict(c) for c in cycles]


def _sync_spots(db: Session, n_member: int, n_assoc: int):
    """Ensure the Spot table has exactly n_member + n_assoc spots with correct types."""
    existing = {s.number: s for s in db.query(Spot).all()}
    total = n_member + n_assoc
    for i in range(1, total + 1):
        spot_type = "member" if i <= n_member else "association"
        if i in existing:
            existing[i].spot_type = spot_type  # fix type if needed
        else:
            db.add(Spot(number=i, spot_type=spot_type, status="active"))
    db.flush()


@router.post("/sync-spots")
def sync_spots(request: Request, db: Session = Depends(get_db)):
    """Sync Spot table to match current settings (total_member_spots + total_assoc_spots). Admin only."""
    _require_admin(request)
    s = db.query(Settings).first()
    if not s:
        raise HTTPException(status_code=500, detail="Settings not configured")
    n_member = s.total_member_spots or 113
    n_assoc  = s.total_assoc_spots  or 5
    _sync_spots(db, n_member, n_assoc)
    db.commit()
    return {"ok": True, "member_spots": n_member, "assoc_spots": n_assoc, "total": n_member + n_assoc}


@router.post("/cycles")
def create_cycle(data: CycleCreate, request: Request, db: Session = Depends(get_db)):
    _require_admin(request)

    start = datetime.fromisoformat(data.start_date)
    gs = db.query(Settings).first()
    if not gs:
        raise HTTPException(status_code=500, detail="Settings not configured")

    # Resolve this cycle's financial settings — use caller-supplied values, else current global defaults.
    # We do NOT mutate global Settings; each cycle stores its own snapshot.
    full_spot_amount     = data.full_spot_amount     if data.full_spot_amount     is not None else gs.full_spot_amount
    half_spot_amount     = data.half_spot_amount     if data.half_spot_amount     is not None else gs.half_spot_amount
    assoc_ded            = data.association_deduction if data.association_deduction is not None else gs.association_deduction
    full_voucher         = data.full_spot_voucher    if data.full_spot_voucher    is not None else getattr(gs, 'full_spot_voucher', 80)
    half_voucher         = data.half_spot_voucher    if data.half_spot_voucher    is not None else getattr(gs, 'half_spot_voucher', 40)
    interval             = getattr(gs, "group_week_interval", 4)

    existing = db.query(Cycle).filter(Cycle.status == "active").first()
    if existing:
        existing.status = "completed"
        existing.end_date = _utcnow()

    # ── Fresh start: reset spot statuses for the new cycle ───────────────────
    db.query(Spot).update({"status": "active"}, synchronize_session=False)
    db.query(Member).filter(Member.status == "received").update(
        {"status": "active"}, synchronize_session=False
    )

    # Association spots are decided when draws start (members must be fully registered first).
    # Only member spots are created at cycle creation; assoc spots added via start-draws.
    n_member = data.total_member_spots if data.total_member_spots else gs.total_member_spots
    n_assoc  = 0   # always 0 at cycle creation

    # Sync Spot table to member spots only
    _sync_spots(db, n_member, 0)

    # Create cycle with its own financial settings snapshot
    cycle = Cycle(
        name=data.name, start_date=start, notes=data.notes, draw_phase="collection",
        full_spot_amount=full_spot_amount,
        half_spot_amount=half_spot_amount,
        association_deduction=assoc_ded,
        full_spot_voucher=full_voucher,
        half_spot_voucher=half_voucher,
        total_member_spots=n_member,
        total_assoc_spots=0,
        group_week_interval=interval,
    )
    db.add(cycle)
    db.flush()

    # Create weeks for member spots only — assoc spot weeks appended when draws start
    gross = n_member * full_spot_amount
    assoc = n_member * assoc_ded
    net   = gross - assoc
    for i in range(1, n_member + 1):
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
def start_draws(cycle_id: int, data: StartDrawsData, request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    cycle = db.query(Cycle).filter(Cycle.id == cycle_id).first()
    if not cycle:
        raise HTTPException(status_code=404, detail="Cycle not found")
    if cycle.draw_phase == "active":
        raise HTTPException(status_code=400, detail="Draws already started")

    n_member = cycle.total_member_spots or 0
    n_assoc  = data.total_assoc_spots

    # Add association spots to the Spot table and append their weeks
    if n_assoc > 0:
        _sync_spots(db, n_member, n_assoc)
        cycle.total_assoc_spots = n_assoc

        # Recalculate pot using actual member assignments (members are now fully registered)
        gross, assoc_amt, net = _calculate_pot(db, cycle_id=cycle.id)

        # Find last existing week to continue the date sequence
        last_week = (db.query(Week)
                     .filter(Week.cycle_id == cycle.id)
                     .order_by(Week.week_number.desc())
                     .first())
        last_num  = last_week.week_number if last_week else n_member
        last_date = last_week.draw_date   if last_week else cycle.start_date

        for i in range(1, n_assoc + 1):
            draw_date = last_date + timedelta(weeks=i)
            # Snap to Sunday
            days_to_sunday = (6 - draw_date.weekday()) % 7
            if days_to_sunday:
                draw_date = draw_date + timedelta(days=days_to_sunday)
            db.add(Week(
                cycle_id=cycle.id,
                week_number=last_num + i,
                draw_date=draw_date,
                is_group_week=False,   # assoc spot weeks are always sale events, never group weeks
                gross_pot=gross, association_amount=assoc_amt, net_pot=net,
            ))

        # Also recalculate all pending member weeks now that final membership is known
        pending_weeks = db.query(Week).filter(
            Week.cycle_id == cycle.id, Week.status == "pending",
            Week.week_number <= n_member
        ).all()
        for w in pending_weeks:
            w.gross_pot = gross
            w.association_amount = assoc_amt
            w.net_pot = net

    cycle.draw_phase = "active"
    cycle.draw_start_week = data.at_week_number
    cycle.draw_started_at = _utcnow()
    db.commit()

    total_weeks = db.query(Week).filter(Week.cycle_id == cycle.id).count()
    return {
        "ok": True,
        "draw_start_week": data.at_week_number,
        "total_assoc_spots": n_assoc,
        "total_weeks": total_weeks,
        "message": (
            f"Draws started from week {data.at_week_number}. "
            + (f"{n_assoc} association spot week(s) added (weeks {n_member+1}–{n_member+n_assoc}). " if n_assoc else "")
            + f"{data.at_week_number} batch draws are now available."
        ),
    }


@router.post("/cycles/{cycle_id}/recalculate-pot")
def recalculate_pot(cycle_id: int, request: Request, db: Session = Depends(get_db)):
    """
    Recalculate gross/assoc/net pot for all pending weeks in a cycle using
    the actual current member assignments (full vs half per spot).
    Only pending weeks are updated — drawn/sold weeks are left as recorded.
    Admin only.
    """
    _require_admin(request)
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
    cycle = db.query(Cycle).filter(Cycle.id == cycle_id).first()
    gs    = db.query(Settings).first()
    cfg   = cycle_cfg(cycle, gs)
    return [week_to_dict(w, cfg) for w in weeks]


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
        if not sa.is_active or sa.cycle_id != w.cycle_id:
            continue
        s = _check_fully_paid(sa.member, w.week_number, db, cycle_id=w.cycle_id)
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

    # Check member status eligibility
    ineligible_reason = None
    if m.status == "left":
        ineligible_reason = f"{m.name} has left the group"
    elif m.status == "received":
        ineligible_reason = f"{m.name} already received a pot this cycle"
    elif m.status != "active":
        ineligible_reason = f"{m.name} is not an active member"

    s = _check_fully_paid(m, w.week_number, db, cycle_id=w.cycle_id)
    if not s["fully_paid"] and not ineligible_reason:
        ineligible_reason = f"Has {s['unpaid_count']} unpaid week(s)"

    eligible = ineligible_reason is None
    return {
        "member_id": member_id, "name": m.name, "week_number": w.week_number,
        "eligible": eligible, "ineligible_reason": ineligible_reason,
        **s,
    }


# ── Single draw ───────────────────────────────────────────────────────────────

@router.get("/weeks/{week_id}")
def get_week(week_id: int, db: Session = Depends(get_db)):
    w = db.query(Week).filter(Week.id == week_id).first()
    if not w:
        raise HTTPException(status_code=404, detail="Week not found")
    return week_to_dict(w)


@router.post("/weeks/{week_id}/draw")
def record_draw(week_id: int, data: DrawResult, request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
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

    # Guard: block draw if all member spots in this cycle have already received a pot
    active_spots_left = db.query(Spot).filter(
        Spot.status == "active", Spot.spot_type == "member"
    ).count()
    if active_spots_left == 0:
        raise HTTPException(
            status_code=400,
            detail="All member spots have already received a pot this cycle. No further draws possible."
        )

    spot = db.query(Spot).filter(Spot.id == data.winner_spot_id).first()
    if not spot:
        raise HTTPException(status_code=404, detail="Spot not found")

    # Association spot → must go through sell endpoint (profit tracked)
    if spot.spot_type == "association":
        raise HTTPException(
            status_code=400,
            detail="Association spot must be sold, not drawn directly. Use the sell endpoint."
        )

    # All members of winning spot (in this cycle) must be active and fully paid
    for sa in [sa for sa in spot.spot_assignments if sa.is_active and sa.cycle_id == w.cycle_id]:
        if sa.member.status == "left":
            raise HTTPException(
                status_code=400,
                detail=f"{sa.member.name} has left the group and cannot receive a pot draw."
            )
        s = _check_fully_paid(sa.member, w.week_number, db, cycle_id=w.cycle_id)
        if not s["fully_paid"]:
            raise HTTPException(
                status_code=400,
                detail=f"{sa.member.name} has {s['unpaid_count']} unpaid week(s) "
                       f"(weeks {s['unpaid_weeks']}). Pot is on hold until full payment."
            )

    w.winner_spot_id = data.winner_spot_id
    w.status = "drawn"
    spot.status = "received"
    winners = []
    for sa in spot.spot_assignments:
        if sa.is_active and sa.cycle_id == w.cycle_id:
            sa.member.status = "received"
            winners.append(sa.member)

    db.commit()
    # Notify winners via SMS
    try:
        from routers.notifications import send_draw_winner
        for member in winners:
            send_draw_winner(w, member, db)
    except Exception:
        pass
    return week_to_dict(w)


# ── Batch draw (when admin starts draws) ─────────────────────────────────────

@router.post("/batch-draw")
def record_batch_draw(data: BatchDrawResult, request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
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
            if sa.is_active and sa.cycle_id == w.cycle_id:
                sa.member.status = "received"
        results.append({"week_id": week_id, "week_number": w.week_number,
                        "winner_spot": spot.number, "status": "drawn"})
    db.commit()
    return {"processed": len([r for r in results if r["status"] == "drawn"]),
            "results": results}


# ── Pot sale ──────────────────────────────────────────────────────────────────

@router.post("/weeks/{week_id}/sell")
def record_sale(week_id: int, data: PotSale, request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
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

    pay_status = _check_fully_paid(buyer, w.week_number, db, cycle_id=w.cycle_id)
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

    # Mark buyer's week payment as paid — they settled by acquiring the pot
    buyer_payment = db.query(Payment).filter(
        Payment.member_id == buyer.id,
        Payment.week_id == week_id,
    ).first()
    if buyer_payment and buyer_payment.status != "paid":
        buyer_payment.status = "paid"
        buyer_payment.payment_method = "pot_sale"
        buyer_payment.paid_date = _utcnow()
        buyer_payment.reference = f"Pot purchase week {w.week_number}"

    # Mark buyer's spot as received (current cycle only)
    buyer.status = "received"
    for sa in buyer.spot_assignments:
        if sa.is_active and sa.cycle_id == w.cycle_id:
            sa.spot.status = "received"

    w.status = "sold"
    db.commit()
    return week_to_dict(w)


# ── Active lists for UI ───────────────────────────────────────────────────────

@router.get("/active-spots")
def active_spots(cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Returns only member-type active spots (association spots excluded — use sell endpoint)."""
    spots = (db.query(Spot).filter(Spot.status == "active", Spot.spot_type == "member")
             .order_by(Spot.number).all())
    return [
        {
            "id": s.id, "number": s.number, "type": s.spot_type,
            "members": [
                {"id": sa.member.id, "name": sa.member.name, "share": sa.share}
                for sa in s.spot_assignments
                if sa.is_active and (cycle_id is None or sa.cycle_id == cycle_id)
            ],
            "share": next(
                (sa.share for sa in s.spot_assignments
                 if sa.is_active and (cycle_id is None or sa.cycle_id == cycle_id)),
                "full"
            ),
        }
        for s in spots
    ]


@router.get("/active-members")
def active_members_for_sale(cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    if not cycle_id:
        active = db.query(Cycle).filter(Cycle.status == "active").first()
        cycle_id = active.id if active else None

    if cycle_id:
        member_ids = [r[0] for r in db.query(MemberSpot.member_id).filter(
            MemberSpot.cycle_id == cycle_id, MemberSpot.is_active == True
        ).distinct().all()]
        members = db.query(Member).filter(
            Member.id.in_(member_ids), Member.status == "active"
        ).order_by(Member.name).all()
    else:
        members = db.query(Member).filter(Member.status == "active").order_by(Member.name).all()

    def _member_dict(m):
        sas = [sa for sa in m.spot_assignments
               if sa.is_active and (cycle_id is None or sa.cycle_id == cycle_id)]
        return {
            "id": m.id, "name": m.name,
            "spot_numbers": [sa.spot.number for sa in sas],
            "share": sas[0].share if sas else "full",
            "spot_count": len(sas),
        }
    return [_member_dict(m) for m in members]


@router.get("/cycles/{cycle_id}/closure-checklist")
def closure_checklist(cycle_id: int, db: Session = Depends(get_db)):
    """
    Pre-close health check for a cycle.
    Returns a list of checklist items — each with ok=True/False and a detail message.
    Admin should verify all items are ok=True before closing a cycle.
    """
    cycle = db.query(Cycle).filter(Cycle.id == cycle_id).first()
    if not cycle:
        raise HTTPException(status_code=404, detail="Cycle not found")

    weeks = db.query(Week).filter(Week.cycle_id == cycle_id).all()
    week_ids = [w.id for w in weeks]
    total_weeks = len(weeks)
    drawn_weeks = sum(1 for w in weeks if w.status in ("drawn", "sold"))

    # 1. All weeks drawn/sold?
    all_drawn = drawn_weeks == total_weeks
    items = [{
        "check": "All weeks drawn or sold",
        "ok": all_drawn,
        "detail": f"{drawn_weeks}/{total_weeks} weeks completed"
            + ("" if all_drawn else f" — {total_weeks - drawn_weeks} still pending"),
    }]

    # 2. All members paid up (no pending/late/missed)?
    unpaid_count = db.query(Payment).filter(
        Payment.week_id.in_(week_ids),
        Payment.status.in_(["pending", "late", "missed"])
    ).count() if week_ids else 0
    items.append({
        "check": "All member payments settled",
        "ok": unpaid_count == 0,
        "detail": "All payments settled" if unpaid_count == 0
            else f"{unpaid_count} unpaid payment record(s) remain",
    })

    # 3. All drawn weeks have a disbursement?
    disbursed_week_ids = {r[0] for r in db.query(PotDisbursement.week_id).filter(
        PotDisbursement.week_id.in_(week_ids)
    ).all()} if week_ids else set()
    drawn_week_ids = {w.id for w in weeks if w.status in ("drawn", "sold")}
    undisbursed = drawn_week_ids - disbursed_week_ids
    items.append({
        "check": "All pots disbursed (cheques issued)",
        "ok": len(undisbursed) == 0,
        "detail": "All cheques issued" if not undisbursed
            else f"{len(undisbursed)} drawn week(s) without a cheque record",
    })

    # 4. All vouchers paid to vendor?
    disbs = db.query(PotDisbursement).filter(
        PotDisbursement.week_id.in_(week_ids)
    ).all() if week_ids else []
    unpaid_vouchers = [d for d in disbs if d.voucher_deduction and not d.voucher_paid]
    items.append({
        "check": "All vouchers paid to vendor",
        "ok": len(unpaid_vouchers) == 0,
        "detail": "All vouchers settled" if not unpaid_vouchers
            else f"{len(unpaid_vouchers)} voucher(s) not yet paid to vendor",
    })

    # 5. Association fund distribution planned?
    from database import AssociationExpense
    assoc_expenses = db.query(AssociationExpense).filter(
        AssociationExpense.cycle_id == cycle_id
    ).count()
    items.append({
        "check": "Association fund reviewed (expenses logged)",
        "ok": assoc_expenses > 0 or drawn_weeks == 0,
        "detail": f"{assoc_expenses} expense record(s) logged" if assoc_expenses
            else "No association expenses recorded — confirm fund distribution",
    })

    all_ok = all(i["ok"] for i in items)
    return {
        "cycle_id": cycle_id,
        "cycle_name": cycle.name,
        "cycle_status": cycle.status,
        "all_clear": all_ok,
        "items": items,
    }


@router.post("/cycles/{cycle_id}/reactivate")
def reactivate_cycle(cycle_id: int, request: Request, db: Session = Depends(get_db)):
    """Restore a completed cycle back to active status. Admin only."""
    _require_admin(request)
    cycle = db.query(Cycle).filter(Cycle.id == cycle_id).first()
    if not cycle:
        raise HTTPException(status_code=404, detail="Cycle not found")
    # Close any currently active cycle first
    other = db.query(Cycle).filter(Cycle.status == "active", Cycle.id != cycle_id).first()
    if other:
        other.status = "completed"
        other.end_date = _utcnow()
    cycle.status = "active"
    cycle.end_date = None
    db.commit()
    return {"ok": True, "id": cycle.id, "name": cycle.name, "status": cycle.status}


@router.delete("/cycles/{cycle_id}")
def delete_cycle(cycle_id: int, request: Request, db: Session = Depends(get_db)):
    """Permanently delete a cycle and all its related data. Admin only."""
    _require_admin(request)
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

    # Delete member-spot assignments for this cycle
    db.query(MemberSpot).filter(
        MemberSpot.cycle_id == cycle_id
    ).delete(synchronize_session=False)

    db.flush()  # flush so the MemberSpot deletions are visible for the next query

    # Delete ALL members that now have no spot assignment in any remaining cycle
    # (covers cycle members, never-assigned members, and NULL-cycle legacy members)
    remaining_assigned = {r[0] for r in db.query(MemberSpot.member_id).all()}
    all_member_ids = [r[0] for r in db.query(Member.id).all()]
    orphan_ids = [mid for mid in all_member_ids if mid not in remaining_assigned]
    if orphan_ids:
        from database import NotificationLog as _NLog
        db.query(_NLog).filter(_NLog.member_id.in_(orphan_ids)).delete(synchronize_session=False)
        db.query(Member).filter(Member.id.in_(orphan_ids)).delete(synchronize_session=False)

    cycle_name = cycle.name
    db.flush()
    db.query(Cycle).filter(Cycle.id == cycle_id).delete(synchronize_session=False)
    db.commit()
    return {"ok": True, "deleted": cycle_name}


def _assoc_fund_data(db: Session, cycle_id: Optional[int]) -> dict:
    """Shared computation for association fund totals — used by both summary and settlement endpoints."""
    cycle = db.query(Cycle).filter(Cycle.id == cycle_id).first() if cycle_id else None
    gs = db.query(Settings).first()
    cfg = cycle_cfg(cycle, gs)

    weeks_q = db.query(Week).filter(Week.cycle_id == cycle_id) if cycle_id else db.query(Week)
    completed_weeks = weeks_q.filter(Week.status.in_(["drawn", "sold"])).all()
    completed_week_ids = [w.id for w in completed_weeks]
    weekly_deductions = _actual_assoc_collected(db, completed_week_ids, cycle_id) if cycle_id else 0.0

    tx_q = (db.query(PotTransaction)
            .filter(PotTransaction.transaction_type.in_(["assoc_spot_sale", "group_week_sale"])))
    if cycle_id:
        week_ids = [w.id for w in weeks_q.all()]
        tx_q = tx_q.filter(PotTransaction.week_id.in_(week_ids)) if week_ids else tx_q.filter(False)
    assoc_txs = tx_q.all()
    spot_sales_profit = sum(t.seller_fee or 0 for t in assoc_txs)

    total_fund = weekly_deductions + spot_sales_profit

    exp_q = (db.query(AssociationExpense).filter(AssociationExpense.cycle_id == cycle_id)
             if cycle_id else db.query(AssociationExpense))
    expenses = exp_q.order_by(AssociationExpense.expense_date).all()
    total_expenses = sum(e.amount for e in expenses)
    net_fund = total_fund - total_expenses

    assignments = (db.query(MemberSpot).filter(MemberSpot.cycle_id == cycle_id, MemberSpot.is_active == True).all()
                   if cycle_id else [])
    full_count = sum(1 for a in assignments if a.share == "full")
    half_count = sum(1 for a in assignments if a.share == "half")
    total_shares = full_count * 1.0 + half_count * 0.5
    per_share = round(net_fund / total_shares, 2) if total_shares > 0 else 0

    return {
        "cycle": cycle,
        "cfg": cfg,
        "weeks": weeks_q.all(),
        "completed_weeks": completed_weeks,
        "weekly_deductions": weekly_deductions,
        "assoc_txs": assoc_txs,
        "spot_sales_profit": spot_sales_profit,
        "total_fund": total_fund,
        "expenses": expenses,
        "total_expenses": total_expenses,
        "net_fund": net_fund,
        "assignments": assignments,
        "full_count": full_count,
        "half_count": half_count,
        "total_shares": total_shares,
        "per_share": per_share,
    }


@router.get("/association-fund")
def association_fund(cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    """
    Full association fund summary for a cycle:
      weekly_deductions   = sum of actual assoc deductions for drawn/sold weeks
      spot_sales_profit   = sum of seller_fee for assoc_spot_sale/group_week_sale
      total_fund          = weekly_deductions + spot_sales_profit
      total_expenses      = sum of AssociationExpense.amount for this cycle
      net_fund            = total_fund - total_expenses
      per_full_return     = net_fund / total_spot_shares (1 share per full, 0.5 per half)
      per_half_return     = per_full_return / 2
    """
    if not cycle_id:
        active = db.query(Cycle).filter(Cycle.status == "active").first()
        cycle_id = active.id if active else None

    d = _assoc_fund_data(db, cycle_id)
    per_full_return = d["per_share"]
    per_half_return = round(per_full_return / 2, 2)

    return {
        "cycle_id": cycle_id,
        "cycle_name": d["cycle"].name if d["cycle"] else None,
        "weeks_completed": len(d["completed_weeks"]),
        "weekly_deductions": d["weekly_deductions"],
        "spot_sales_profit": d["spot_sales_profit"],
        "total_fund": d["total_fund"],
        "total_expenses": d["total_expenses"],
        "net_fund": d["net_fund"],
        "member_assignments": {
            "full": d["full_count"],
            "half": d["half_count"],
            "total_shares": d["total_shares"],
        },
        "per_full_member_return": per_full_return,
        "per_half_member_return": per_half_return,
        "expenses": [
            {"id": e.id, "description": e.description, "amount": e.amount,
             "expense_date": e.expense_date.isoformat(), "notes": e.notes}
            for e in d["expenses"]
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
            for t in d["assoc_txs"]
        ],
    }


# ── Association Expenses ──────────────────────────────────────────────────────

@router.post("/association-expenses")
def add_expense(data: ExpenseCreate, request: Request, db: Session = Depends(get_db)):
    """Record an expense deducted from the association fund (paper, pen, etc.)."""
    _require_admin(request)
    cycle = db.query(Cycle).filter(Cycle.id == data.cycle_id).first()
    if not cycle:
        raise HTTPException(status_code=404, detail="Cycle not found")
    expense_date = datetime.fromisoformat(data.expense_date) if data.expense_date else _utcnow()
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
    _require_admin(request)
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
    Return is proportional to share: full=1 share, half=0.5 share.
    """
    cycle = db.query(Cycle).filter(Cycle.id == cycle_id).first()
    if not cycle:
        raise HTTPException(status_code=404, detail="Cycle not found")

    d = _assoc_fund_data(db, cycle_id)
    cfg = d["cfg"]
    per_share = d["per_share"]

    from collections import defaultdict
    member_assignments: dict = defaultdict(list)
    for a in d["assignments"]:
        member_assignments[a.member_id].append(a)

    member_returns = []
    for member_id, member_spots in member_assignments.items():
        member = member_spots[0].member
        shares = sum(1.0 if a.share == "full" else 0.5 for a in member_spots)
        return_amount = round(per_share * shares, 2)
        spots_info = [
            {
                "spot_number": a.spot.number if a.spot else None,
                "share": a.share,
                "contribution_per_week": a.weekly_contribution,
                "association_per_week": (
                    cfg.association_deduction if a.share == "full"
                    else cfg.association_deduction / 2
                ),
            }
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
    weeks = d["weeks"]

    return {
        "cycle_id": cycle_id,
        "cycle_name": cycle.name,
        "weeks_completed": len(d["completed_weeks"]),
        "total_weeks": len(weeks),
        "weekly_deductions": d["weekly_deductions"],
        "spot_sales_profit": d["spot_sales_profit"],
        "total_fund": d["total_fund"],
        "total_expenses": d["total_expenses"],
        "net_fund": d["net_fund"],
        "per_share_return": per_share,
        "per_full_member_return": per_share,
        "per_half_member_return": round(per_share / 2, 2),
        "total_members": len(member_returns),
        "members": member_returns,
    }

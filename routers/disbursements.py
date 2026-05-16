from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func as sqla_func
from pydantic import BaseModel, Field, field_validator
from typing import Optional
from datetime import datetime, timezone
from database import get_db, PotDisbursement, Week, Member, Settings, Spot, Payment, MemberSpot, Cycle, cycle_cfg
from routers.deps import _require_admin


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)

router = APIRouter()


class DisbursementCreate(BaseModel):
    week_id: int
    member_id: Optional[int] = None          # set for half-spot split cheques
    gross_amount: float = Field(..., gt=0)
    service_fee: float = Field(0, ge=0)
    voucher_deduction: float = Field(0, ge=0)
    cheque_number: str
    cheque_date: str
    guarantor_1_id: int
    guarantor_2_id: int
    guarantor_3_id: int
    status: str = "issued"
    notes: Optional[str] = None

    @field_validator("cheque_number")
    @classmethod
    def cheque_number_not_empty(cls, v):
        if not v or not v.strip():
            raise ValueError("Cheque number is required")
        return v.strip()


class DisbursementUpdate(BaseModel):
    status: Optional[str] = None
    cheque_number: Optional[str] = None
    notes: Optional[str] = None


def _to_dict(d: PotDisbursement) -> dict:
    def _m(m):
        return {"id": m.id, "name": m.name} if m else None
    return {
        "id": d.id,
        "week_id": d.week_id,
        "week_number": d.week.week_number if d.week else None,
        "draw_date": d.week.draw_date.isoformat() if d.week else None,
        "winner_spot_id": d.winner_spot_id,
        "winner_spot_number": d.winner_spot.number if d.winner_spot else None,
        "winner_members": (
            [{"id": sa.member.id, "name": sa.member.name}
             for sa in d.winner_spot.spot_assignments
             if sa.is_active and (d.week is None or sa.cycle_id == d.week.cycle_id)]
            if d.winner_spot else
            # Sold without a drawn spot — show the transaction buyer
            ([{"id": d.week.transactions[0].buyer.id, "name": d.week.transactions[0].buyer.name}]
             if d.week and d.week.transactions and d.week.transactions[0].buyer else [])
        ),
        "member_id": d.member_id,
        "member_name": d.member.name if d.member else None,
        "gross_amount": d.gross_amount,
        "service_fee": d.service_fee or 0,
        "voucher_deduction": d.voucher_deduction,
        "net_amount": d.net_amount,
        "cheque_number": d.cheque_number,
        "cheque_date": d.cheque_date.isoformat(),
        "guarantor_1": _m(d.guarantor_1),
        "guarantor_2": _m(d.guarantor_2),
        "guarantor_3": _m(d.guarantor_3),
        "status": d.status,
        "notes": d.notes,
        "created_at": d.created_at.isoformat(),
    }


@router.get("")
def list_disbursements(cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    if not cycle_id:
        active = db.query(Cycle).filter(Cycle.status == "active").first()
        cycle_id = active.id if active else None
    q = db.query(PotDisbursement).join(Week).options(
        joinedload(PotDisbursement.winner_spot).joinedload("spot_assignments").joinedload("member"),
        joinedload(PotDisbursement.week).joinedload("transactions").joinedload("buyer"),
        joinedload(PotDisbursement.member),
        joinedload(PotDisbursement.guarantor_1),
        joinedload(PotDisbursement.guarantor_2),
        joinedload(PotDisbursement.guarantor_3),
    )
    if cycle_id:
        q = q.filter(Week.cycle_id == cycle_id)
    rows = q.order_by(PotDisbursement.id.desc()).all()
    return [_to_dict(d) for d in rows]


@router.get("/week/{week_id}")
def get_disbursement_for_week(week_id: int, db: Session = Depends(get_db)):
    rows = db.query(PotDisbursement).filter(PotDisbursement.week_id == week_id).options(
        joinedload(PotDisbursement.winner_spot).joinedload("spot_assignments").joinedload("member"),
        joinedload(PotDisbursement.week).joinedload("transactions").joinedload("buyer"),
        joinedload(PotDisbursement.member),
        joinedload(PotDisbursement.guarantor_1),
        joinedload(PotDisbursement.guarantor_2),
        joinedload(PotDisbursement.guarantor_3),
    ).all()
    return [_to_dict(d) for d in rows]


@router.get("/voucher-info/{week_id}")
def get_voucher_info(week_id: int, db: Session = Depends(get_db)):
    """
    Return the full deduction breakdown for the winner of this week.

    Net = Gross − Assoc_fund − Service_fee − Voucher
      Gross       = member_count × spot_amount (full or half per member)
      Assoc_fund  = member_count × assoc_deduction (full or half per member)
      Service_fee = winner's weekly amount × 1
      Voucher     = (member_spots + assoc_spots) × voucher_rate (full or half for winner)

    Example (113 member spots, 5 assoc spots, all full, 21,000/1,000/80):
      Gross   = 113 × 21,000 = 2,373,000
      Assoc   = 113 × 1,000  =   113,000
      Net_pot =               2,260,000
      Service =   1 × 21,000 =    21,000
      Voucher = 118 ×     80 =     9,440
      Net     =               2,229,560
    """
    w = db.query(Week).filter(Week.id == week_id).first()
    if not w or not w.winner_spot_id:
        raise HTTPException(status_code=404, detail="Week not found or no winner")

    cycle = db.query(Cycle).filter(Cycle.id == w.cycle_id).first()
    gs    = db.query(Settings).first()
    cfg   = cycle_cfg(cycle, gs)

    total_spots = cfg.total_member_spots + cfg.total_assoc_spots
    # Total weeks members actually pay into (includes worker week if present)
    total_weeks = db.query(Week).filter(Week.cycle_id == w.cycle_id).count()

    # Winner's pot deduction = FULL voucher for ALL spots (assoc collects full amount)
    full_voucher_total = cfg.full_spot_voucher * total_spots
    half_voucher_total = cfg.half_spot_voucher * total_spots

    # Vendor payment = only members who actually paid this week
    # (missing members did not attend — vendor does not receive for them)
    paid_payments = db.query(Payment).filter(
        Payment.week_id == week_id,
        Payment.status == "paid"
    ).all()
    vendor_full_count = 0
    vendor_half_count = 0
    for pmt in paid_payments:
        for ms in db.query(MemberSpot).filter(
            MemberSpot.member_id == pmt.member_id,
            MemberSpot.cycle_id == w.cycle_id,
            MemberSpot.is_active == True
        ).all():
            if ms.share == "full":
                vendor_full_count += 1
            else:
                vendor_half_count += 1
    vendor_payment = (vendor_full_count * cfg.full_spot_voucher
                      + vendor_half_count * cfg.half_spot_voucher)
    vendor_paid_spots = vendor_full_count + vendor_half_count

    assignments = [sa for sa in w.winner_spot.spot_assignments
                   if sa.is_active and sa.cycle_id == w.cycle_id]

    total_service_fee = sum(
        cfg.full_spot_amount if sa.share == "full" else cfg.half_spot_amount
        for sa in assignments
    )
    total_voucher = sum(
        full_voucher_total if sa.share == "full" else half_voucher_total
        for sa in assignments
    )

    net_after_all = (w.net_pot or 0) - total_service_fee - total_voucher

    assoc_deduction     = cfg.association_deduction
    assoc_per_week_full = assoc_deduction
    assoc_per_week_half = assoc_deduction / 2
    # Association total uses total_weeks (every payment week, including worker week)
    assoc_total_full    = assoc_per_week_full * total_weeks
    assoc_total_half    = assoc_per_week_half * total_weeks

    return {
        "week_id": week_id,
        "total_spots": total_spots,
        "total_weeks": total_weeks,
        "gross_pot": w.gross_pot,
        "association_amount": w.association_amount,
        "net_pot": w.net_pot,
        "association_deduction_per_spot": assoc_deduction,
        "full_spot_voucher_rate": cfg.full_spot_voucher,
        "half_spot_voucher_rate": cfg.half_spot_voucher,
        "full_voucher_total": full_voucher_total,
        "half_voucher_total": half_voucher_total,
        "service_fee": total_service_fee,
        "voucher_deduction": total_voucher,
        "net_after_all": net_after_all,
        "vendor_payment": vendor_payment,
        "vendor_paid_spots": vendor_paid_spots,
        "assoc_retains": total_voucher - vendor_payment,
        "assignments": [
            {
                "member_id": sa.member_id,
                "member": sa.member.name,
                "share": sa.share,
                "service_fee": cfg.full_spot_amount if sa.share == "full" else cfg.half_spot_amount,
                "voucher": full_voucher_total if sa.share == "full" else half_voucher_total,
                "association_per_week": assoc_per_week_full if sa.share == "full" else assoc_per_week_half,
                "association_total_cycle": assoc_total_full if sa.share == "full" else assoc_total_half,
            }
            for sa in assignments
        ],
    }


@router.post("")
def create_disbursement(data: DisbursementCreate, request: Request, db: Session = Depends(get_db)):
    _require_admin(request)

    w = db.query(Week).filter(Week.id == data.week_id).first()
    if not w:
        raise HTTPException(status_code=404, detail="Week not found")
    if w.status not in ("drawn", "sold"):
        raise HTTPException(status_code=400, detail="Week must be drawn or sold first")
    # Sold weeks may not have a drawn winner (sold from pending) — still allow disbursement
    if not w.winner_spot_id and w.status != "sold":
        raise HTTPException(status_code=400, detail="No winner recorded for this week")

    if data.member_id:
        # Half-spot split: check if this specific member already has a cheque for this week
        existing = db.query(PotDisbursement).filter(
            PotDisbursement.week_id == data.week_id,
            PotDisbursement.member_id == data.member_id
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail="Disbursement already recorded for this member")
    else:
        # Full spot: block if any disbursement exists for this week
        existing = db.query(PotDisbursement).filter(
            PotDisbursement.week_id == data.week_id
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail="Disbursement already recorded for this week")

    guarantor_ids = [data.guarantor_1_id, data.guarantor_2_id, data.guarantor_3_id]

    # Guarantors must be distinct
    if len(set(guarantor_ids)) < 3:
        raise HTTPException(status_code=400, detail="All three guarantors must be different people")

    # Identify recipient members (to block them from being their own guarantor)
    winner_member_ids = set()
    if w.winner_spot:
        winner_member_ids = {
            sa.member_id for sa in w.winner_spot.spot_assignments
            if sa.is_active and sa.cycle_id == w.cycle_id
        }
    elif w.status == "sold" and w.transactions:
        # Sold without draw — block the buyer from being their own guarantor
        winner_member_ids = {w.transactions[0].buyer_id}

    for gid in guarantor_ids:
        g = db.query(Member).filter(Member.id == gid).first()
        if not g:
            raise HTTPException(status_code=404, detail=f"Guarantor member {gid} not found")
        if gid in winner_member_ids:
            raise HTTPException(
                status_code=400,
                detail=f"{g.name} is the pot winner and cannot be their own guarantor"
            )
        if g.status == "left":
            raise HTTPException(
                status_code=400,
                detail=f"{g.name} has left the group and cannot act as guarantor"
            )

    # ── Service fee: auto-calculated when winner spot is known; use form value for sold-without-draw ──
    cycle = db.query(Cycle).filter(Cycle.id == w.cycle_id).first()
    gs    = db.query(Settings).first()
    cfg   = cycle_cfg(cycle, gs)
    if w.winner_spot_id:
        assignments = [sa for sa in w.winner_spot.spot_assignments
                       if sa.is_active and sa.cycle_id == w.cycle_id]
        if data.member_id:
            member_sa = next((sa for sa in assignments if sa.member_id == data.member_id), None)
            if not member_sa:
                raise HTTPException(status_code=400, detail="member_id is not an active winner for this week")
            service_fee = cfg.half_spot_amount if member_sa.share == "half" else cfg.full_spot_amount
        else:
            service_fee = sum(
                cfg.full_spot_amount if sa.share == "full" else cfg.half_spot_amount
                for sa in assignments
            )
    else:
        # Sold without draw — accept service_fee from the request body as-is
        service_fee = data.service_fee

    # ── Cash sufficiency check ────────────────────────────────────────────────
    cycle_week_ids = [r[0] for r in db.query(Week.id).filter(Week.cycle_id == w.cycle_id).all()]
    total_collected = db.query(sqla_func.sum(Payment.amount)).filter(
        Payment.week_id.in_(cycle_week_ids), Payment.status == "paid"
    ).scalar() or 0.0
    already_disbursed = db.query(sqla_func.sum(PotDisbursement.gross_amount)).filter(
        PotDisbursement.week_id.in_(cycle_week_ids)
    ).scalar() or 0.0
    available = total_collected - already_disbursed
    if round(data.gross_amount, 2) > round(available + 0.005, 2):
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient funds: {available:,.0f} ETB available, "
                   f"{data.gross_amount:,.0f} ETB requested. "
                   f"Ensure more members have paid before disbursing."
        )

    # For sold weeks: deduct seller/association fee split equally per recipient
    seller_fee_deduction = 0.0
    if w.status == "sold" and w.transactions:
        total_seller_fee = w.transactions[0].seller_fee or 0.0
        if w.winner_spot_id:
            n_recipients = len([sa for sa in w.winner_spot.spot_assignments
                                if sa.is_active and sa.cycle_id == w.cycle_id]) or 1
        else:
            n_recipients = 1
        seller_fee_deduction = total_seller_fee / n_recipients
    net_amount = data.gross_amount - service_fee - (data.voucher_deduction or 0) - seller_fee_deduction
    # For sold-without-draw weeks winner_spot_id may be null — that is valid
    d = PotDisbursement(
        week_id=data.week_id,
        member_id=data.member_id,
        winner_spot_id=w.winner_spot_id,  # None for group/assoc-sale weeks
        gross_amount=data.gross_amount,
        service_fee=service_fee,
        voucher_deduction=data.voucher_deduction or 0,
        net_amount=net_amount,
        cheque_number=data.cheque_number,
        cheque_date=datetime.fromisoformat(data.cheque_date),
        guarantor_1_id=data.guarantor_1_id,
        guarantor_2_id=data.guarantor_2_id,
        guarantor_3_id=data.guarantor_3_id,
        status=data.status,
        notes=data.notes,
    )
    db.add(d)
    db.commit()
    db.refresh(d)
    # Notify the cheque recipient that their cheque is ready
    try:
        from routers.notifications import send_disbursement_ready
        if data.member_id:
            recipient = db.query(Member).filter(Member.id == data.member_id).first()
            if recipient:
                send_disbursement_ready(w, recipient, data.cheque_number, db)
        elif w.winner_spot:
            for sa in w.winner_spot.spot_assignments:
                if sa.is_active and sa.cycle_id == w.cycle_id:
                    send_disbursement_ready(w, sa.member, data.cheque_number, db)
        elif w.transactions:
            buyer = db.query(Member).filter(Member.id == w.transactions[0].buyer_id).first()
            if buyer:
                send_disbursement_ready(w, buyer, data.cheque_number, db)
    except Exception:
        pass
    return _to_dict(d)


@router.put("/{disbursement_id}")
def update_disbursement(disbursement_id: int, data: DisbursementUpdate,
                        request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    d = db.query(PotDisbursement).filter(PotDisbursement.id == disbursement_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Disbursement not found")
    for field, val in data.model_dump(exclude_none=True).items():
        setattr(d, field, val)
    db.commit()
    db.refresh(d)
    return _to_dict(d)

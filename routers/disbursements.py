from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field, field_validator
from typing import Optional
from datetime import datetime
from database import get_db, PotDisbursement, Week, Member, Settings, Spot

router = APIRouter()


class DisbursementCreate(BaseModel):
    week_id: int
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
        "winner_members": [
            {"id": sa.member.id, "name": sa.member.name}
            for sa in d.winner_spot.spot_assignments
            if sa.is_active and (d.week is None or sa.cycle_id == d.week.cycle_id)
        ] if d.winner_spot else [],
        "gross_amount": d.gross_amount,
        "service_fee": getattr(d, "service_fee", 0) or 0,
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
def list_disbursements(db: Session = Depends(get_db)):
    rows = db.query(PotDisbursement).order_by(PotDisbursement.id.desc()).all()
    return [_to_dict(d) for d in rows]


@router.get("/week/{week_id}")
def get_disbursement_for_week(week_id: int, db: Session = Depends(get_db)):
    d = db.query(PotDisbursement).filter(PotDisbursement.week_id == week_id).first()
    if not d:
        return None
    return _to_dict(d)


@router.get("/voucher-info/{week_id}")
def get_voucher_info(week_id: int, db: Session = Depends(get_db)):
    """
    Return the full deduction breakdown for the winner of this week.
    Formula (full spot example with 118 weeks, 113 member spots):
      gross     = 118 × 21,000 = 2,478,000
      assoc     = 113 × 1,000  =   113,000   (already in net_pot)
      service   =  1  × 21,000 =    21,000   (funds worker week)
      voucher   = 118 ×     80 =     9,440
      net       = 2,478,000 − 113,000 − 21,000 − 9,440 = 2,334,560
    """
    w = db.query(Week).filter(Week.id == week_id).first()
    if not w or not w.winner_spot_id:
        raise HTTPException(status_code=404, detail="Week not found or no winner")

    settings = db.query(Settings).first()
    # Total weeks in this cycle (including worker week if present)
    total_weeks = db.query(Week).filter(Week.cycle_id == w.cycle_id).count()

    full_voucher_total = getattr(settings, "full_spot_voucher", 80) * total_weeks
    half_voucher_total = getattr(settings, "half_spot_voucher", 40) * total_weeks

    assignments = [sa for sa in w.winner_spot.spot_assignments
                   if sa.is_active and sa.cycle_id == w.cycle_id]

    # Service fee = one week's contribution amount per share type
    total_service_fee = sum(
        settings.full_spot_amount if sa.share == "full" else settings.half_spot_amount
        for sa in assignments
    )
    total_voucher = sum(
        full_voucher_total if sa.share == "full" else half_voucher_total
        for sa in assignments
    )

    net_after_all = (w.net_pot or 0) - total_service_fee - total_voucher

    # Association contribution per member per week
    assoc_deduction = getattr(settings, "association_deduction", 1000)
    # Total association contributed by this spot over the whole cycle
    assoc_per_week_full = assoc_deduction            # 1000
    assoc_per_week_half = assoc_deduction / 2        # 500
    assoc_total_full = assoc_per_week_full * total_weeks
    assoc_total_half = assoc_per_week_half * total_weeks

    return {
        "week_id": week_id,
        "total_weeks": total_weeks,
        "gross_pot": w.gross_pot,
        "association_amount": w.association_amount,
        "net_pot": w.net_pot,
        "association_deduction_per_spot": assoc_deduction,
        "full_spot_voucher_rate": getattr(settings, "full_spot_voucher", 80),
        "half_spot_voucher_rate": getattr(settings, "half_spot_voucher", 40),
        "full_voucher_total": full_voucher_total,
        "half_voucher_total": half_voucher_total,
        "service_fee": total_service_fee,
        "voucher_deduction": total_voucher,
        "net_after_all": net_after_all,
        "assignments": [
            {
                "member": sa.member.name,
                "share": sa.share,
                "service_fee": settings.full_spot_amount if sa.share == "full" else settings.half_spot_amount,
                "voucher": full_voucher_total if sa.share == "full" else half_voucher_total,
                # Association contribution breakdown per member
                "association_per_week": assoc_per_week_full if sa.share == "full" else assoc_per_week_half,
                "association_total_cycle": assoc_total_full if sa.share == "full" else assoc_total_half,
            }
            for sa in assignments
        ],
    }


@router.post("")
def create_disbursement(data: DisbursementCreate, request: Request, db: Session = Depends(get_db)):
    if getattr(request.state, "user_role", None) != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    w = db.query(Week).filter(Week.id == data.week_id).first()
    if not w:
        raise HTTPException(status_code=404, detail="Week not found")
    if w.status not in ("drawn", "sold"):
        raise HTTPException(status_code=400, detail="Week must be drawn or sold first")
    if not w.winner_spot_id:
        raise HTTPException(status_code=400, detail="No winner recorded for this week")

    existing = db.query(PotDisbursement).filter(
        PotDisbursement.week_id == data.week_id
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Disbursement already recorded for this week")

    guarantor_ids = [data.guarantor_1_id, data.guarantor_2_id, data.guarantor_3_id]

    # Guarantors must be distinct
    if len(set(guarantor_ids)) < 3:
        raise HTTPException(status_code=400, detail="All three guarantors must be different people")

    # Identify winner members (to block them from being their own guarantor)
    winner_member_ids = {
        sa.member_id for sa in w.winner_spot.spot_assignments
        if sa.is_active and sa.cycle_id == w.cycle_id
    } if w.winner_spot else set()

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

    net_amount = data.gross_amount - (data.service_fee or 0) - (data.voucher_deduction or 0)
    d = PotDisbursement(
        week_id=data.week_id,
        winner_spot_id=w.winner_spot_id,
        gross_amount=data.gross_amount,
        service_fee=data.service_fee or 0,
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
    # Notify winner(s) that their cheque is ready
    try:
        from routers.notifications import send_disbursement_ready
        winner_sas = w.winner_spot.spot_assignments if w.winner_spot else []
        for sa in winner_sas:
            if sa.is_active and sa.cycle_id == w.cycle_id:
                send_disbursement_ready(w, sa.member, data.cheque_number, db)
    except Exception:
        pass
    return _to_dict(d)


@router.put("/{disbursement_id}")
def update_disbursement(disbursement_id: int, data: DisbursementUpdate,
                        request: Request, db: Session = Depends(get_db)):
    if getattr(request.state, "user_role", None) != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    d = db.query(PotDisbursement).filter(PotDisbursement.id == disbursement_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Disbursement not found")
    for field, val in data.model_dump(exclude_none=True).items():
        setattr(d, field, val)
    db.commit()
    db.refresh(d)
    return _to_dict(d)

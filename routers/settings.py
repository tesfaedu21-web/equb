from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from database import get_db, Settings

router = APIRouter()


class SettingsUpdate(BaseModel):
    full_spot_amount: Optional[float] = None
    half_spot_amount: Optional[float] = None
    association_deduction: Optional[float] = None
    group_week_interval: Optional[int] = None
    full_spot_voucher: Optional[float] = None
    half_spot_voucher: Optional[float] = None
    include_worker_slot: Optional[bool] = None
    group_name: Optional[str] = None
    group_tagline: Optional[str] = None
    logo_url: Optional[str] = None


@router.get("")
def get_settings(db: Session = Depends(get_db)):
    s = db.query(Settings).first()
    if not s:
        return {}
    return {
        "full_spot_amount": s.full_spot_amount,
        "half_spot_amount": s.half_spot_amount,
        "association_deduction": s.association_deduction,
        "total_member_spots": s.total_member_spots,
        "total_assoc_spots": s.total_assoc_spots,
        "group_week_interval": s.group_week_interval,
        "full_spot_voucher": s.full_spot_voucher,
        "half_spot_voucher": s.half_spot_voucher,
        "include_worker_slot": s.include_worker_slot,
        "group_name": s.group_name,
        "group_tagline": s.group_tagline,
        "logo_url": s.logo_url,
    }


@router.put("")
def update_settings(data: SettingsUpdate, request: Request, db: Session = Depends(get_db)):
    if getattr(request.state, "user_role", None) != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    s = db.query(Settings).first()
    if not s:
        raise HTTPException(status_code=404, detail="Settings not found")
    for field, val in data.model_dump(exclude_none=True).items():
        setattr(s, field, val)
    db.commit()
    db.refresh(s)
    return get_settings(db)

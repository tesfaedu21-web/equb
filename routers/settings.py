from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field, model_validator, field_validator
from typing import Optional
from database import get_db, Settings
from routers.deps import _require_superadmin

router = APIRouter()


class SettingsUpdate(BaseModel):
    full_spot_amount: Optional[float] = Field(None, gt=0)
    half_spot_amount: Optional[float] = Field(None, gt=0)
    association_deduction: Optional[float] = Field(None, ge=0)
    group_week_interval: Optional[int] = Field(None, ge=2)
    full_spot_voucher: Optional[float] = Field(None, ge=0)
    half_spot_voucher: Optional[float] = Field(None, ge=0)
    group_name: Optional[str] = None
    group_tagline: Optional[str] = None
    logo_url: Optional[str] = None

    @field_validator("logo_url", mode="before")
    @classmethod
    def validate_logo_url(cls, v):
        if v is None or v == "":
            return None
        if not v.startswith(("http://", "https://")):
            raise ValueError("logo_url must be an http or https URL")
        if len(v) > 2048:
            raise ValueError("logo_url is too long (max 2048 characters)")
        return v

    @model_validator(mode="after")
    def half_less_than_full(self):
        if self.full_spot_amount and self.half_spot_amount:
            if self.half_spot_amount >= self.full_spot_amount:
                raise ValueError("half_spot_amount must be less than full_spot_amount")
        return self


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
        "group_name": s.group_name,
        "group_tagline": s.group_tagline,
        "logo_url": s.logo_url,
    }


@router.put("")
def update_settings(data: SettingsUpdate, request: Request, db: Session = Depends(get_db)):
    _require_superadmin(request)
    s = db.query(Settings).first()
    if not s:
        raise HTTPException(status_code=404, detail="Settings not found")
    for field, val in data.model_dump(exclude_none=True).items():
        setattr(s, field, val)
    db.commit()
    db.refresh(s)
    return get_settings(db)

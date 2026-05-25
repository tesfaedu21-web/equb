import secrets
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request, Header
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from database import get_db, SmsQueue, NotificationSettings, NotificationLog

logger = logging.getLogger("equb.sms_gateway")
router = APIRouter()


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _auth(db: Session, x_device_token: Optional[str]) -> NotificationSettings:
    cfg = db.query(NotificationSettings).first()
    if not cfg or not cfg.device_token:
        raise HTTPException(status_code=403, detail="Android gateway not configured")
    if x_device_token != cfg.device_token:
        raise HTTPException(status_code=403, detail="Invalid device token")
    return cfg


# ── Android app polls this every 30s ─────────────────────────────────────────

@router.get("/pending")
def get_pending(
    db: Session = Depends(get_db),
    x_device_token: Optional[str] = Header(default=None),
):
    _auth(db, x_device_token)
    jobs = (db.query(SmsQueue)
            .filter(SmsQueue.status == "pending", SmsQueue.attempts < 3)
            .order_by(SmsQueue.created_at)
            .limit(10)
            .all())
    for j in jobs:
        j.attempts += 1
    db.commit()
    return [{"id": j.id, "phone": j.phone, "message": j.message} for j in jobs]


# ── Android app reports result ────────────────────────────────────────────────

class AckRequest(BaseModel):
    id: int
    status: str        # "sent" | "failed"
    response: Optional[str] = ""


@router.post("/ack")
def ack_job(
    data: AckRequest,
    db: Session = Depends(get_db),
    x_device_token: Optional[str] = Header(default=None),
):
    _auth(db, x_device_token)
    job = db.query(SmsQueue).filter(SmsQueue.id == data.id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job.status = data.status
    job.sent_at = _utcnow()
    job.provider_response = data.response or ""

    # Update the linked notification log if one exists
    log = (db.query(NotificationLog)
           .filter(NotificationLog.provider_response == f"sgw:{job.id}")
           .first())
    if log:
        log.status = data.status
        log.provider_response = data.response or ""

    db.commit()
    return {"ok": True}


# ── Admin: generate a new device token ───────────────────────────────────────

@router.post("/device-token")
def generate_device_token(request: Request, db: Session = Depends(get_db)):
    role = getattr(request.state, "user_role", "")
    if role not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="Admin only")
    cfg = db.query(NotificationSettings).first()
    cfg.device_token = secrets.token_urlsafe(32)
    db.commit()
    return {"device_token": cfg.device_token}


# ── Admin: view queue status ──────────────────────────────────────────────────

@router.get("/queue")
def queue_status(request: Request, db: Session = Depends(get_db)):
    role = getattr(request.state, "user_role", "")
    if role not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="Admin only")

    total   = db.query(SmsQueue).count()
    pending = db.query(SmsQueue).filter(SmsQueue.status == "pending").count()
    sent    = db.query(SmsQueue).filter(SmsQueue.status == "sent").count()
    failed  = db.query(SmsQueue).filter(SmsQueue.status == "failed").count()
    recent  = (db.query(SmsQueue)
               .order_by(SmsQueue.created_at.desc())
               .limit(20).all())
    return {
        "total": total, "pending": pending, "sent": sent, "failed": failed,
        "recent": [
            {"id": j.id, "phone": j.phone, "status": j.status,
             "attempts": j.attempts, "created_at": j.created_at.isoformat() if j.created_at else None,
             "sent_at": j.sent_at.isoformat() if j.sent_at else None}
            for j in recent
        ],
    }

import json
import urllib.request
import urllib.parse
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
from database import (get_db, Member, Payment, Week, Cycle,
                      NotificationSettings, NotificationTemplate, NotificationLog)

router = APIRouter()


# ── Models ──────────────────────────────────────────────────────────────────

class SettingsUpdate(BaseModel):
    provider: Optional[str] = None
    api_key: Optional[str] = None
    username: Optional[str] = None
    sender_id: Optional[str] = None
    is_active: Optional[bool] = None


class TemplateUpdate(BaseModel):
    title: Optional[str] = None
    message: Optional[str] = None
    is_active: Optional[bool] = None


class SendRequest(BaseModel):
    member_ids: List[int]
    template_key: str
    extra: Optional[dict] = {}        # override / extra placeholders


class BroadcastRequest(BaseModel):
    template_key: str
    week_id: Optional[int] = None     # for payment_reminder / missed_payment


# ── SMS sending ──────────────────────────────────────────────────────────────

def _send_sms(phone: str, message: str, cfg: NotificationSettings) -> tuple[str, str]:
    """
    Returns (status, provider_response).
    If is_active=False uses 'mock' mode — logs without sending.
    """
    if not cfg.is_active:
        return "mock", f"[MOCK] Would send to {phone}: {message[:60]}…"

    if cfg.provider == "africastalking":
        return _send_africastalking(phone, message, cfg)

    return "failed", f"Unknown provider: {cfg.provider}"


def _send_africastalking(phone: str, message: str, cfg: NotificationSettings) -> tuple[str, str]:
    try:
        data = urllib.parse.urlencode({
            "username": cfg.username or "sandbox",
            "to": phone,
            "message": message,
            **({"from": cfg.sender_id} if cfg.sender_id else {}),
        }).encode()
        req = urllib.request.Request(
            "https://api.africastalking.com/version1/messaging",
            data=data,
            headers={
                "apiKey": cfg.api_key or "",
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
            return "sent", body
    except Exception as e:
        return "failed", str(e)


def _render(template: str, vars: dict) -> str:
    for k, v in vars.items():
        template = template.replace("{" + k + "}", str(v))
    return template


def _member_vars(m: Member, db: Session, week_number: int = 9999) -> dict:
    unpaid = (db.query(Payment).join(Week)
              .filter(Payment.member_id == m.id,
                      Payment.status.in_(["pending", "late", "missed"]),
                      Week.week_number <= week_number).all())
    return {
        "member_name": m.name,
        "unpaid_count": str(len(unpaid)),
        "unpaid_amount": str(int(sum(p.amount for p in unpaid))),
        "weeks_list": ", ".join(str(p.week.week_number) for p in unpaid),
    }


# ── Auto-send on payment confirmed ───────────────────────────────────────────

def send_payment_confirmed(payment, db: Session) -> None:
    """Fire-and-forget SMS when a payment is marked paid. Silently skips if no phone or SMS inactive."""
    try:
        m = payment.member
        w = payment.week
        if not m or not m.phone or not w:
            return
        cfg = db.query(NotificationSettings).first()
        if not cfg:
            return
        tmpl = db.query(NotificationTemplate).filter_by(key="payment_confirmed").first()
        if not tmpl or not tmpl.is_active:
            return
        method_label = {"cash": "Cash", "bank_transfer": "Bank Transfer",
                        "cheque": "Cheque"}.get(payment.payment_method or "", "Cash")
        vars_ = {
            "member_name": m.name,
            "amount": str(int(payment.amount)),
            "week_number": str(w.week_number),
            "draw_date": w.draw_date.strftime("%d %b %Y"),
            "payment_method": method_label,
        }
        msg = _render(tmpl.message, vars_)
        status, response = _send_sms(m.phone, msg, cfg)
        db.add(NotificationLog(
            member_id=m.id, phone=m.phone,
            template_key="payment_confirmed", message=msg,
            status=status, provider_response=response,
        ))
        db.commit()
    except Exception:
        pass   # never break the payment flow


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/settings")
def get_settings(db: Session = Depends(get_db)):
    cfg = db.query(NotificationSettings).first()
    return {
        "provider": cfg.provider,
        "api_key": "***" if cfg.api_key else None,
        "username": cfg.username,
        "sender_id": cfg.sender_id,
        "is_active": cfg.is_active,
    }


@router.put("/settings")
def update_settings(data: SettingsUpdate, db: Session = Depends(get_db)):
    cfg = db.query(NotificationSettings).first()
    if data.provider is not None:
        cfg.provider = data.provider
    if data.api_key is not None:
        cfg.api_key = data.api_key
    if data.username is not None:
        cfg.username = data.username
    if data.sender_id is not None:
        cfg.sender_id = data.sender_id
    if data.is_active is not None:
        cfg.is_active = data.is_active
    db.commit()
    return {"ok": True}


@router.get("/templates")
def get_templates(db: Session = Depends(get_db)):
    return [
        {"id": t.id, "key": t.key, "title": t.title,
         "message": t.message, "is_active": t.is_active}
        for t in db.query(NotificationTemplate).all()
    ]


@router.put("/templates/{template_id}")
def update_template(template_id: int, data: TemplateUpdate, db: Session = Depends(get_db)):
    t = db.query(NotificationTemplate).filter(NotificationTemplate.id == template_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Template not found")
    if data.title is not None:
        t.title = data.title
    if data.message is not None:
        t.message = data.message
    if data.is_active is not None:
        t.is_active = data.is_active
    db.commit()
    return {"ok": True}


@router.post("/send")
def send_to_members(data: SendRequest, db: Session = Depends(get_db)):
    tmpl = db.query(NotificationTemplate).filter_by(key=data.template_key).first()
    if not tmpl:
        raise HTTPException(status_code=404, detail="Template not found")
    cfg = db.query(NotificationSettings).first()

    results = []
    for mid in data.member_ids:
        m = db.query(Member).filter(Member.id == mid).first()
        if not m or not m.phone:
            results.append({"member_id": mid, "status": "skipped", "reason": "no phone"})
            continue

        vars_ = _member_vars(m, db)
        vars_.update(data.extra or {})
        msg = _render(tmpl.message, vars_)
        status, response = _send_sms(m.phone, msg, cfg)

        log = NotificationLog(
            member_id=m.id, phone=m.phone,
            template_key=data.template_key, message=msg,
            status=status, provider_response=response,
        )
        db.add(log)
        results.append({"member_id": mid, "name": m.name, "phone": m.phone,
                        "status": status})

    db.commit()
    sent = sum(1 for r in results if r["status"] in ("sent", "mock"))
    return {"sent": sent, "total": len(data.member_ids), "results": results}


@router.post("/broadcast/payment-reminder")
def broadcast_payment_reminder(week_id: int, db: Session = Depends(get_db)):
    """Send reminder to all members with pending/late payment for a specific week."""
    w = db.query(Week).filter(Week.id == week_id).first()
    if not w:
        raise HTTPException(status_code=404, detail="Week not found")

    tmpl = db.query(NotificationTemplate).filter_by(key="payment_reminder").first()
    cfg = db.query(NotificationSettings).first()

    pending = (db.query(Payment)
               .filter(Payment.week_id == week_id,
                       Payment.status.in_(["pending", "late"]))
               .all())

    results = []
    for p in pending:
        m = p.member
        if not m or not m.phone:
            continue
        vars_ = {
            "member_name": m.name,
            "amount": str(int(p.amount)),
            "week_number": str(w.week_number),
            "draw_date": w.draw_date.strftime("%d %b %Y"),
        }
        msg = _render(tmpl.message, vars_)
        status, response = _send_sms(m.phone, msg, cfg)
        log = NotificationLog(
            member_id=m.id, phone=m.phone,
            template_key="payment_reminder", message=msg,
            status=status, provider_response=response,
        )
        db.add(log)
        results.append({"member_id": m.id, "name": m.name, "status": status})

    db.commit()
    return {"sent": len(results), "results": results}


@router.post("/broadcast/missed-payments")
def broadcast_missed_payments(db: Session = Depends(get_db)):
    """Send missed payment notice to all members with any unpaid weeks."""
    tmpl = db.query(NotificationTemplate).filter_by(key="missed_payment").first()
    cfg = db.query(NotificationSettings).first()

    members = db.query(Member).filter(Member.status == "active").all()
    results = []
    for m in members:
        if not m.phone:
            continue
        vars_ = _member_vars(m, db)
        if vars_["unpaid_count"] == "0":
            continue
        msg = _render(tmpl.message, vars_)
        status, response = _send_sms(m.phone, msg, cfg)
        log = NotificationLog(
            member_id=m.id, phone=m.phone,
            template_key="missed_payment", message=msg,
            status=status, provider_response=response,
        )
        db.add(log)
        results.append({"member_id": m.id, "name": m.name,
                        "unpaid": vars_["unpaid_count"], "status": status})

    db.commit()
    return {"sent": len(results), "results": results}


@router.get("/logs")
def notification_logs(limit: int = 100, db: Session = Depends(get_db)):
    logs = (db.query(NotificationLog)
            .order_by(NotificationLog.sent_at.desc())
            .limit(limit).all())
    return [
        {
            "id": l.id,
            "member_name": l.member.name if l.member else None,
            "phone": l.phone,
            "template_key": l.template_key,
            "message": l.message,
            "status": l.status,
            "sent_at": l.sent_at.isoformat(),
        }
        for l in logs
    ]

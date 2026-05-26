import logging
import re
import uuid
import urllib.request
import urllib.parse
from fastapi import APIRouter, Depends, HTTPException, Request

logger = logging.getLogger("equb.notifications")
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from database import (get_db, Member, Payment, Week, Cycle, MemberSpot,
                      NotificationSettings, NotificationTemplate, NotificationLog, SmsQueue)
from routers.deps import _require_feature

router = APIRouter()


def _batch_id():
    return uuid.uuid4().hex[:12]


# ── Models ──────────────────────────────────────────────────────────────────

class SettingsUpdate(BaseModel):
    provider: Optional[str] = None
    api_key: Optional[str] = None
    username: Optional[str] = None
    sender_id: Optional[str] = None
    is_active: Optional[bool] = None
    sms_language: Optional[str] = None   # en | am | both
    # Email / SMTP
    email_enabled: Optional[bool] = None
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_use_tls: Optional[bool] = None
    email_from: Optional[str] = None


class EmailSendRequest(BaseModel):
    member_ids: List[int]
    subject: str
    body: str                    # plain-text body
    body_html: Optional[str] = None  # optional HTML version


class TemplateUpdate(BaseModel):
    title: Optional[str] = None
    message: Optional[str] = None
    message_am: Optional[str] = None
    is_active: Optional[bool] = None


class SendRequest(BaseModel):
    member_ids: List[int]
    template_key: str
    extra: Optional[dict] = {}        # override / extra placeholders


class BroadcastRequest(BaseModel):
    template_key: str
    week_id: Optional[int] = None     # for payment_reminder / missed_payment


# ── SMS sending ──────────────────────────────────────────────────────────────

def _normalize_ethiopian_phone(phone: str) -> str:
    """Convert Ethiopian phone numbers to +251XXXXXXXXX format."""
    phone = phone.strip().replace(" ", "").replace("-", "")
    if phone.startswith("+251"):
        return phone
    if phone.startswith("251") and len(phone) == 12:
        return "+" + phone
    if phone.startswith("0") and len(phone) == 10:
        return "+251" + phone[1:]
    return phone  # return as-is if unrecognized format


def _send_sms(phone: str, message: str, cfg: NotificationSettings, db=None, **kwargs) -> tuple[str, str]:
    """
    Returns (status, provider_response).
    If is_active=False uses 'mock' mode — logs without sending.
    """
    if not cfg.is_active:
        return "mock", f"[MOCK] Would send to {phone}: {message[:60]}…"

    if cfg.provider == "africastalking":
        return _send_africastalking(phone, message, cfg)

    if cfg.provider == "android_gateway":
        return _queue_for_android(phone, message, cfg, db, **kwargs)

    return "failed", f"Unknown provider: {cfg.provider}"


def _queue_for_android(phone: str, message: str, cfg: NotificationSettings, db, **kwargs) -> tuple[str, str]:
    if db is None:
        return "failed", "android_gateway requires db session"
    phone = _normalize_ethiopian_phone(phone)
    job = SmsQueue(
        phone=phone,
        message=message,
        template_key=kwargs.get("template_key"),
        member_id=kwargs.get("member_id"),
    )
    db.add(job)
    db.flush()  # get the id before commit
    return "queued", f"sgw:{job.id}"


def _send_africastalking(phone: str, message: str, cfg: NotificationSettings) -> tuple[str, str]:
    try:
        phone = _normalize_ethiopian_phone(phone)
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
            # Extract messageId for delivery tracking
            try:
                import json as _json
                parsed = _json.loads(body)
                recipients = parsed.get("SMSMessageData", {}).get("Recipients", [])
                if recipients:
                    msg_id = recipients[0].get("messageId", "")
                    return "sent", f"{msg_id}|{body}"
            except Exception:
                pass
            return "sent", body
    except Exception as e:
        return "failed", str(e)


def _send_email(to_address: str, subject: str, body: str, cfg, body_html: str = None) -> tuple[str, str]:
    """Send email via SMTP. Returns (status, detail)."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    if not cfg.email_enabled:
        return "mock", f"[MOCK EMAIL] To: {to_address} Subject: {subject}"
    if not cfg.smtp_host or not cfg.smtp_user or not cfg.smtp_password:
        return "failed", "SMTP not fully configured (host/user/password required)"
    try:
        from_addr = cfg.email_from or cfg.smtp_user
        if body_html:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = from_addr
            msg["To"] = to_address
            msg.attach(MIMEText(body, "plain", "utf-8"))
            msg.attach(MIMEText(body_html, "html", "utf-8"))
        else:
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = subject
            msg["From"] = from_addr
            msg["To"] = to_address
        port = cfg.smtp_port or 587
        if cfg.smtp_use_tls:
            server = smtplib.SMTP(cfg.smtp_host, port, timeout=15)
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(cfg.smtp_host, port, timeout=15)
        server.login(cfg.smtp_user, cfg.smtp_password)
        server.sendmail(from_addr, [to_address], msg.as_bytes())
        server.quit()
        return "sent", f"Delivered to {to_address}"
    except Exception as e:
        return "failed", str(e)


_ET_EPOCH = 1723856
_ET_MONTHS = ["መስከረም", "ጥቅምት", "ህዳር", "ታህሳስ", "ጥር", "የካቲት",
               "መጋቢት", "ሚያዚያ", "ግንቦት", "ሰኔ", "ሐምሌ", "ነሐሴ", "ጳጉሜ"]

_PAYMENT_METHOD_EN = {
    "cash": "Cash",
    "bank_transfer": "Bank Transfer",
    "cheque": "Cheque",
    "pot_sale": "Pot Sale",
}
_PAYMENT_METHOD_AM = {
    "cash": "ጥሬ ገንዘብ",
    "bank_transfer": "የባንክ ዝውውር",
    "cheque": "ቼክ",
}


def _to_et_date(date_str: str) -> str:
    """Convert a Gregorian date string to Ethiopian calendar (e.g. '15 ጥቅምት 2017')."""
    try:
        from datetime import datetime
        dt = None
        for fmt in ("%Y-%m-%d", "%d %b %Y", "%d/%m/%Y", "%B %d, %Y"):
            try:
                dt = datetime.strptime(str(date_str).strip(), fmt)
                break
            except ValueError:
                continue
        if dt is None:
            return date_str
        y, m, d = dt.year, dt.month, dt.day
        a = (14 - m) // 12
        yy = y + 4800 - a
        mm = m + 12 * a - 3
        jdn = d + (153 * mm + 2) // 5 + 365 * yy + yy // 4 - yy // 100 + yy // 400 - 32045
        r = (jdn - _ET_EPOCH) % 1461
        n = r % 365 + 365 * (r // 1460)
        et_year = 4 * ((jdn - _ET_EPOCH) // 1461) + (r // 365) - (r // 1460)
        et_month = (n // 30) + 1
        et_day = (n % 30) + 1
        month_name = _ET_MONTHS[et_month - 1] if 1 <= et_month <= 13 else str(et_month)
        return f"{et_day} {month_name} {et_year}"
    except Exception:
        return date_str


def _render(template: str, vars: dict) -> str:
    for k, v in vars.items():
        template = template.replace("{" + k + "}", str(v))
    remaining = re.findall(r"\{(\w+)\}", template)
    if remaining:
        logger.warning("unreplaced template variables: %s", remaining)
    return template


def _pick_message(tmpl, cfg, vars_: dict) -> str:
    """Return rendered message in the configured language (en / am / both)."""
    lang = (cfg.sms_language or "en") if cfg else "en"
    en_vars = vars_
    am_vars = vars_
    if lang in ("am", "both"):
        overrides = {}
        if vars_.get("draw_date"):
            overrides["draw_date"] = _to_et_date(str(vars_["draw_date"]))
        pm_key = vars_.get("_pm_key")
        if pm_key:
            overrides["payment_method"] = _PAYMENT_METHOD_AM.get(pm_key, vars_.get("payment_method", pm_key))
        if overrides:
            am_vars = {**vars_, **overrides}
    en_msg = _render(tmpl.message, en_vars)
    am_msg = _render(tmpl.message_am, am_vars) if tmpl.message_am else _render(tmpl.message, am_vars)
    if lang == "am":
        return am_msg
    if lang == "both" and tmpl.message_am:
        return f"{en_msg}\n\n{am_msg}"
    return en_msg


def _member_vars(m: Member, db: Session, week_number: int = 9999, cycle_id: Optional[int] = None) -> dict:
    q = (db.query(Payment).join(Week)
         .filter(Payment.member_id == m.id,
                 Payment.status.in_(["pending", "late", "missed"]),
                 Week.week_number <= week_number))
    if cycle_id:
        q = q.filter(Week.cycle_id == cycle_id)
    unpaid = q.all()
    return {
        "member_name": m.name,
        "unpaid_count": str(len(unpaid)),
        "unpaid_amount": f"{int(sum(p.amount for p in unpaid)):,}",
        "weeks_list": ", ".join(str(p.week.week_number) for p in unpaid),
    }


# ── Auto-send on payment confirmed ───────────────────────────────────────────

def send_payment_confirmed(payment, db: Session) -> str:
    """
    Send SMS when a payment is marked paid.
    Returns SMS status: 'sent' | 'mock' | 'failed' | 'skipped'.
    Never raises — payment flow must not be broken by SMS issues.
    """
    try:
        m = payment.member
        w = payment.week
        if not m or not m.phone or not w:
            return "skipped"
        cfg = db.query(NotificationSettings).first()
        if not cfg:
            return "skipped"
        tmpl = db.query(NotificationTemplate).filter_by(key="payment_confirmed").first()
        if not tmpl or not tmpl.is_active:
            return "skipped"
        pm_key = payment.payment_method or "cash"
        method_label = _PAYMENT_METHOD_EN.get(pm_key, "Cash")
        vars_ = {
            "member_name": m.name,
            "amount": f"{int(payment.amount):,}",
            "week_number": str(w.week_number),
            "draw_date": w.draw_date.strftime("%d %b %Y"),
            "payment_method": method_label,
            "_pm_key": pm_key,
        }
        msg = _pick_message(tmpl, cfg, vars_)
        status, response = _send_sms(m.phone, msg, cfg, db=db,
                                     template_key="payment_confirmed", member_id=m.id)
        db.add(NotificationLog(
            member_id=m.id, phone=m.phone,
            template_key="payment_confirmed", message=msg,
            status=status, provider_response=response,
        ))
        db.commit()
        return status
    except Exception:
        return "skipped"  # never break the payment flow


# ── Auto-send on missed payment ──────────────────────────────────────────────

def send_missed_payment(payment, db: Session) -> str:
    """Send SMS when a payment is auto-marked as missed. Never raises."""
    try:
        m = payment.member
        w = payment.week
        if not m or not m.phone or not w:
            return "skipped"
        cfg = db.query(NotificationSettings).first()
        if not cfg:
            return "skipped"
        tmpl = db.query(NotificationTemplate).filter_by(key="missed_payment").first()
        if not tmpl or not tmpl.is_active:
            return "skipped"
        vars_ = {
            "member_name": m.name,
            "week_number": str(w.week_number),
            "amount": f"{int(payment.amount):,}",
            "draw_date": w.draw_date.strftime("%d %b %Y"),
            "unpaid_count": "1",
        }
        msg = _pick_message(tmpl, cfg, vars_)
        status, response = _send_sms(m.phone, msg, cfg, db=db,
                                     template_key="missed_payment", member_id=m.id)
        db.add(NotificationLog(
            member_id=m.id, phone=m.phone,
            template_key="missed_payment", message=msg,
            status=status, provider_response=response,
        ))
        db.commit()
        return status
    except Exception:
        return "skipped"


# ── Auto-send on draw winner assigned ────────────────────────────────────────

def send_draw_winner(week, member, db: Session) -> str:
    """Send SMS to draw winner. Never raises."""
    try:
        if not member or not member.phone:
            return "skipped"
        cfg = db.query(NotificationSettings).first()
        if not cfg:
            return "skipped"
        tmpl = db.query(NotificationTemplate).filter_by(key="draw_winner").first()
        if not tmpl or not tmpl.is_active:
            return "skipped"
        vars_ = {
            "member_name": member.name,
            "week_number": str(week.week_number),
            "draw_date": week.draw_date.strftime("%d %b %Y"),
            "net_pot": str(int(week.net_pot or 0)),
        }
        msg = _pick_message(tmpl, cfg, vars_)
        status, response = _send_sms(member.phone, msg, cfg, db=db,
                                     template_key="draw_winner", member_id=member.id)
        db.add(NotificationLog(
            member_id=member.id, phone=member.phone,
            template_key="draw_winner", message=msg,
            status=status, provider_response=response,
        ))
        return status
    except Exception:
        return "skipped"


# ── Draw announcement to all members ─────────────────────────────────────────

def send_draw_announcement(week, spot_number: int, db: Session) -> int:
    """Broadcast draw result (spot number only) to every member with a phone."""
    try:
        cfg = db.query(NotificationSettings).first()
        if not cfg:
            return 0
        tmpl = db.query(NotificationTemplate).filter_by(key="draw_announcement").first()
        if not tmpl or not tmpl.is_active:
            return 0
        vars_ = {
            "week_number": str(week.week_number),
            "spot_number": str(spot_number),
            "draw_date":   week.draw_date.strftime("%d %b %Y"),
        }
        from database import Member as _Member, MemberSpot as _MemberSpot
        members = (
            db.query(_Member)
            .join(_MemberSpot, _MemberSpot.member_id == _Member.id)
            .filter(_MemberSpot.cycle_id == week.cycle_id, _MemberSpot.is_active == True)
            .filter(_Member.phone.isnot(None), _Member.phone != "")
            .distinct()
            .all()
        )
        sent = 0
        bid = _batch_id()
        for m in members:
            try:
                msg = _pick_message(tmpl, cfg, vars_)
                status, response = _send_sms(m.phone, msg, cfg, db=db,
                                             template_key="draw_announcement",
                                             member_id=m.id, batch_id=bid)
                db.add(NotificationLog(
                    member_id=m.id, phone=m.phone,
                    template_key="draw_announcement", message=msg,
                    status=status, provider_response=response, batch_id=bid,
                ))
                if status == "sent":
                    sent += 1
            except Exception:
                pass
        db.commit()
        return sent
    except Exception:
        return 0


# ── Auto-send on disbursement created ────────────────────────────────────────

def send_disbursement_ready(week, member, cheque_number: str, db: Session) -> str:
    """Send SMS when cheque/disbursement is recorded. Never raises."""
    try:
        if not member or not member.phone:
            return "skipped"
        cfg = db.query(NotificationSettings).first()
        if not cfg:
            return "skipped"
        tmpl = db.query(NotificationTemplate).filter_by(key="disbursement_ready").first()
        if not tmpl or not tmpl.is_active:
            return "skipped"
        vars_ = {
            "member_name": member.name,
            "week_number": str(week.week_number),
            "cheque_number": cheque_number or "—",
        }
        msg = _pick_message(tmpl, cfg, vars_)
        status, response = _send_sms(member.phone, msg, cfg, db=db,
                                     template_key="disbursement_ready", member_id=member.id)
        db.add(NotificationLog(
            member_id=member.id, phone=member.phone,
            template_key="disbursement_ready", message=msg,
            status=status, provider_response=response,
        ))
        return status
    except Exception:
        return "skipped"


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/settings")
def get_settings(request: Request, db: Session = Depends(get_db)):
    _require_feature(request, db, "notifications")
    cfg = db.query(NotificationSettings).first()
    return {
        "provider": cfg.provider,
        "api_key": "***" if cfg.api_key else None,
        "username": cfg.username,
        "sender_id": cfg.sender_id,
        "is_active": cfg.is_active,
        "has_device_token": bool(cfg.device_token),
        "sms_language": cfg.sms_language or "en",
        "email_enabled": bool(cfg.email_enabled),
        "smtp_host": cfg.smtp_host or "",
        "smtp_port": cfg.smtp_port or 587,
        "smtp_user": cfg.smtp_user or "",
        "smtp_password": "***" if cfg.smtp_password else "",
        "smtp_use_tls": cfg.smtp_use_tls if cfg.smtp_use_tls is not None else True,
        "email_from": cfg.email_from or "",
    }


@router.put("/settings")
def update_settings(data: SettingsUpdate, request: Request, db: Session = Depends(get_db)):
    _require_feature(request, db, "notifications")
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
    if data.sms_language is not None:
        cfg.sms_language = data.sms_language
    if data.email_enabled is not None:
        cfg.email_enabled = data.email_enabled
    if data.smtp_host is not None:
        cfg.smtp_host = data.smtp_host
    if data.smtp_port is not None:
        cfg.smtp_port = data.smtp_port
    if data.smtp_user is not None:
        cfg.smtp_user = data.smtp_user
    if data.smtp_password is not None and data.smtp_password != "***":
        cfg.smtp_password = data.smtp_password
    if data.smtp_use_tls is not None:
        cfg.smtp_use_tls = data.smtp_use_tls
    if data.email_from is not None:
        cfg.email_from = data.email_from
    db.commit()
    return {"ok": True}


@router.get("/templates")
def get_templates(db: Session = Depends(get_db)):
    return [
        {"id": t.id, "key": t.key, "title": t.title,
         "message": t.message, "message_am": t.message_am or "", "is_active": t.is_active}
        for t in db.query(NotificationTemplate).all()
    ]


@router.put("/templates/{template_id}")
def update_template(template_id: int, data: TemplateUpdate, request: Request, db: Session = Depends(get_db)):
    _require_feature(request, db, "notifications")
    t = db.query(NotificationTemplate).filter(NotificationTemplate.id == template_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Template not found")
    if data.title is not None:
        t.title = data.title
    if data.message is not None:
        t.message = data.message
    if data.message_am is not None:
        t.message_am = data.message_am
    if data.is_active is not None:
        t.is_active = data.is_active
    db.commit()
    return {"ok": True}


@router.post("/send")
def send_to_members(data: SendRequest, request: Request, db: Session = Depends(get_db)):
    _require_feature(request, db, "notifications")
    tmpl = db.query(NotificationTemplate).filter_by(key=data.template_key).first()
    if not tmpl:
        raise HTTPException(status_code=404, detail="Template not found")
    cfg = db.query(NotificationSettings).first()

    active = db.query(Cycle).filter(Cycle.status == "active").first()
    active_cycle_id = active.id if active else None

    bid = _batch_id() if len(data.member_ids) > 1 else None
    results = []
    for mid in data.member_ids:
        m = db.query(Member).filter(Member.id == mid).first()
        if not m or not m.phone:
            results.append({"member_id": mid, "status": "skipped", "reason": "no phone"})
            continue

        vars_ = _member_vars(m, db, cycle_id=active_cycle_id)
        vars_.update(data.extra or {})
        msg = _pick_message(tmpl, cfg, vars_)
        status, response = _send_sms(m.phone, msg, cfg, db=db,
                                     template_key=data.template_key, member_id=m.id)

        log = NotificationLog(
            member_id=m.id, phone=m.phone,
            template_key=data.template_key, message=msg,
            status=status, provider_response=response,
            batch_id=bid,
        )
        db.add(log)
        results.append({"member_id": mid, "name": m.name, "phone": m.phone,
                        "status": status})

    db.commit()
    sent = sum(1 for r in results if r["status"] in ("sent", "mock"))
    return {"sent": sent, "total": len(data.member_ids), "results": results}


@router.post("/broadcast/payment-reminder")
def broadcast_payment_reminder(week_id: int, request: Request, db: Session = Depends(get_db)):
    _require_feature(request, db, "notifications")
    """Send reminder to all members with pending/late payment for a specific week."""
    w = db.query(Week).filter(Week.id == week_id).first()
    if not w:
        raise HTTPException(status_code=404, detail="Week not found")

    tmpl = db.query(NotificationTemplate).filter_by(key="payment_reminder").first()
    if not tmpl or not tmpl.is_active:
        raise HTTPException(status_code=400, detail="Payment reminder template is disabled or not found")
    cfg = db.query(NotificationSettings).first()
    if not cfg:
        raise HTTPException(status_code=500, detail="Notification settings not configured")

    pending = (db.query(Payment)
               .filter(Payment.week_id == week_id,
                       Payment.status.in_(["pending", "late"]))
               .all())

    bid = _batch_id()
    results = []
    for p in pending:
        m = p.member
        if not m or not m.phone:
            continue
        vars_ = {
            "member_name": m.name,
            "amount": f"{int(p.amount):,}",
            "week_number": str(w.week_number),
            "draw_date": w.draw_date.strftime("%d %b %Y"),
        }
        msg = _pick_message(tmpl, cfg, vars_)
        status, response = _send_sms(m.phone, msg, cfg, db=db,
                                     template_key="payment_reminder", member_id=m.id)
        log = NotificationLog(
            member_id=m.id, phone=m.phone,
            template_key="payment_reminder", message=msg,
            status=status, provider_response=response,
            batch_id=bid,
        )
        db.add(log)
        results.append({"member_id": m.id, "name": m.name, "status": status})

    db.commit()
    return {"sent": len(results), "results": results}


@router.post("/broadcast/missed-payments")
def broadcast_missed_payments(request: Request, db: Session = Depends(get_db)):
    _require_feature(request, db, "notifications")
    """Send missed payment notice to all members with any unpaid weeks in the active cycle."""
    tmpl = db.query(NotificationTemplate).filter_by(key="missed_payment").first()
    if not tmpl or not tmpl.is_active:
        raise HTTPException(status_code=400, detail="Missed payment template is disabled or not found")
    cfg = db.query(NotificationSettings).first()
    if not cfg:
        raise HTTPException(status_code=500, detail="Notification settings not configured")

    active = db.query(Cycle).filter(Cycle.status == "active").first()
    cycle_id = active.id if active else None

    if cycle_id:
        member_ids = [r[0] for r in db.query(MemberSpot.member_id).filter(
            MemberSpot.cycle_id == cycle_id, MemberSpot.is_active == True
        ).distinct().all()]
        members = db.query(Member).filter(
            Member.id.in_(member_ids), Member.status == "active"
        ).all()
    else:
        members = db.query(Member).filter(Member.status == "active").all()

    bid = _batch_id()
    results = []
    for m in members:
        if not m.phone:
            continue
        vars_ = _member_vars(m, db, cycle_id=cycle_id)
        if vars_["unpaid_count"] == "0":
            continue
        msg = _pick_message(tmpl, cfg, vars_)
        status, response = _send_sms(m.phone, msg, cfg, db=db,
                                     template_key="missed_payment", member_id=m.id)
        log = NotificationLog(
            member_id=m.id, phone=m.phone,
            template_key="missed_payment", message=msg,
            status=status, provider_response=response,
            batch_id=bid,
        )
        db.add(log)
        results.append({"member_id": m.id, "name": m.name,
                        "unpaid": vars_["unpaid_count"], "status": status})

    db.commit()
    return {"sent": len(results), "results": results}


@router.get("/logs")
def notification_logs(request: Request, db: Session = Depends(get_db)):
    _require_feature(request, db, "notifications")
    """Return broadcast batches (grouped) and individual notifications separately.

    High-volume types (payment_confirmed, payment_reminder, missed_payment) are
    grouped by day even when they have no batch_id, so the list never gets long.
    True one-offs (draw_winner, disbursement_ready) stay as individual rows.
    """
    all_logs = (db.query(NotificationLog)
                .order_by(NotificationLog.sent_at.desc())
                .limit(2000).all())

    # These fire per-member automatically — group by day to avoid flooding the list
    GROUP_BY_DAY = {"payment_confirmed", "payment_reminder", "missed_payment"}

    def _entry(l):
        return {
            "id": l.id,
            "member_name": l.member.name if l.member else None,
            "phone": l.phone,
            "status": l.status,
            "sent_at": l.sent_at.isoformat(),
            "error": l.provider_response if l.status == "failed" else None,
        }

    def _tally(b, status):
        b["total"] += 1
        if status == "sent":    b["sent"]   += 1
        elif status == "failed": b["failed"] += 1
        elif status == "mock":   b["mock"]   += 1

    batch_map: dict = {}   # keyed by real batch_id
    day_map: dict = {}     # keyed by (template_key, "YYYY-MM-DD")
    individuals = []

    for l in all_logs:
        if l.batch_id:
            # Real broadcast batch
            if l.batch_id not in batch_map:
                batch_map[l.batch_id] = {
                    "batch_id": l.batch_id,
                    "template_key": l.template_key,
                    "sent_at": l.sent_at.isoformat(),
                    "total": 0, "sent": 0, "failed": 0, "mock": 0,
                    "logs": [],
                }
            b = batch_map[l.batch_id]
            _tally(b, l.status)
            b["logs"].append(_entry(l))
        elif l.template_key in GROUP_BY_DAY:
            # Group high-volume individual logs by (type, day)
            day = l.sent_at.strftime("%Y-%m-%d") if l.sent_at else "unknown"
            key = (l.template_key, day)
            if key not in day_map:
                day_map[key] = {
                    "batch_id": f"day_{l.template_key}_{day}",
                    "template_key": l.template_key,
                    "sent_at": l.sent_at.isoformat(),
                    "total": 0, "sent": 0, "failed": 0, "mock": 0,
                    "logs": [],
                }
            b = day_map[key]
            _tally(b, l.status)
            b["logs"].append(_entry(l))
        else:
            # True one-off (draw_winner, disbursement_ready, etc.)
            individuals.append({**_entry(l), "template_key": l.template_key})

    # Only collapse day-groups that have more than 1 member — single sends stay individual
    day_batches = []
    for g in day_map.values():
        if g["total"] > 1:
            day_batches.append(g)
        else:
            log = g["logs"][0]
            individuals.append({**log, "template_key": g["template_key"]})

    all_batches = list(batch_map.values()) + day_batches
    batches = sorted(all_batches, key=lambda b: b["sent_at"], reverse=True)
    individuals_sorted = sorted(individuals, key=lambda l: l["sent_at"], reverse=True)
    return {"batches": batches, "individuals": individuals_sorted[:50]}


# ── Email endpoints ───────────────────────────────────────────────────────────

@router.post("/email/send")
def send_email_to_members(data: EmailSendRequest, request: Request,
                          db: Session = Depends(get_db)):
    """Send a custom email to selected members (those with an email address)."""
    _require_feature(request, db, "notifications")
    cfg = db.query(NotificationSettings).first()
    if not cfg:
        raise HTTPException(500, "Notification settings not configured")

    bid = _batch_id() if len(data.member_ids) > 1 else None
    results = []
    for mid in data.member_ids:
        m = db.query(Member).filter(Member.id == mid).first()
        if not m:
            results.append({"member_id": mid, "status": "skipped", "reason": "not found"})
            continue
        if not m.email:
            results.append({"member_id": mid, "name": m.name, "status": "skipped", "reason": "no email"})
            continue
        status, detail = _send_email(m.email, data.subject, data.body, cfg, body_html=data.body_html)
        db.add(NotificationLog(
            member_id=m.id, phone=m.email,
            template_key="email_custom", message=data.subject,
            status=status, provider_response=detail, batch_id=bid,
        ))
        results.append({"member_id": mid, "name": m.name, "email": m.email, "status": status})

    db.commit()
    sent = sum(1 for r in results if r["status"] in ("sent", "mock"))
    return {"sent": sent, "total": len(data.member_ids), "results": results}


@router.post("/email/test")
def test_email(request: Request, db: Session = Depends(get_db)):
    """Send a test email to the configured smtp_user address."""
    _require_feature(request, db, "notifications")
    cfg = db.query(NotificationSettings).first()
    if not cfg:
        raise HTTPException(500, "Notification settings not configured")
    target = cfg.email_from or cfg.smtp_user
    if not target:
        raise HTTPException(400, "No email_from or smtp_user configured")
    status, detail = _send_email(
        target,
        "Equb — Email Test",
        "This is a test email from your Equb system. Email notifications are working correctly.",
        cfg,
    )
    return {"status": status, "detail": detail, "sent_to": target}

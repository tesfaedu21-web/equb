import logging
import os
import time
import secrets
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session

from database import init_db, get_db, User, Settings, Payment, Week, _pwd, log_action
from routers.deps import _get_permissions
from routers import (
    members, draws, payments, reports, notifications,
    auth          as auth_router,
    settings      as settings_router,
    disbursements as disbursements_router,
    sms_gateway   as sms_gateway_router,
    debts         as debts_router,
    marketplace   as marketplace_router,
    portal        as portal_router,
    backup        as backup_router,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("equb")

# ── Environment ───────────────────────────────────────────────────────────────
_db_url = os.environ.get("DATABASE_URL", "")
IS_PRODUCTION = bool(
    os.environ.get("RAILWAY_ENVIRONMENT") or
    (_db_url.startswith("postgresql") and "localhost" not in _db_url and "127.0.0.1" not in _db_url)
)

_sk_env = os.environ.get("SECRET_KEY")
if not _sk_env and IS_PRODUCTION:
    raise RuntimeError(
        "SECRET_KEY environment variable is required in production. "
        "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
    )
SECRET_KEY = _sk_env or "dev-only-insecure-key-set-SECRET_KEY-in-production"

# ── Rate limiting (in-memory, per IP) ────────────────────────────────────────
_login_attempts: dict = defaultdict(list)   # ip -> [unix timestamps]
_RATE_WINDOW   = 300   # 5-minute window
_RATE_MAX      = 5     # max attempts per window
_LOCKOUT_SEC   = 600   # 10-minute lockout after exceeding

def _prune_attempts():
    """Remove stale IP entries to prevent unbounded memory growth."""
    cutoff = time.time() - (_RATE_WINDOW + _LOCKOUT_SEC)
    stale = [ip for ip, ts in _login_attempts.items() if all(t < cutoff for t in ts)]
    for ip in stale:
        del _login_attempts[ip]

def _is_rate_limited(ip: str) -> bool:
    now = time.time()
    attempts = [t for t in _login_attempts[ip] if now - t < _RATE_WINDOW]
    _login_attempts[ip] = attempts
    _prune_attempts()
    return len(attempts) >= _RATE_MAX

def _record_attempt(ip: str):
    _login_attempts[ip].append(time.time())

def _clear_attempts(ip: str):
    _login_attempts.pop(ip, None)

# ── Session blocklist (server-side invalidation) ──────────────────────────────
_invalidated_sessions: dict = {}  # token → expiry timestamp (float)
_SESSION_TTL = 86400              # 24 h — matches max realistic session lifetime

def _invalidate_session(token: str):
    now = time.time()
    _invalidated_sessions[token] = now + _SESSION_TTL
    # Prune expired entries periodically; avoids clear-all which would un-blocklist
    # tokens that were invalidated less than 24 h ago.
    if len(_invalidated_sessions) > 1000:
        expired = [k for k, v in _invalidated_sessions.items() if v < now]
        for k in expired:
            del _invalidated_sessions[k]

# ── Utilities ─────────────────────────────────────────────────────────────────
def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        # Railway's edge proxy appends the real client IP as the rightmost entry.
        # Using [0] (leftmost) lets a client spoof their own X-Forwarded-For header.
        return forwarded.split(",")[-1].strip()
    return request.client.host or "unknown"

def _utcnow() -> datetime:
    """Naive UTC datetime — compatible with naive datetimes stored in the DB."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

# ── Scheduled job functions ───────────────────────────────────────────────────
async def auto_close_past_weeks():
    """
    Nightly at 21:00 UTC (midnight EAT):
      pending → late   if draw_date passed but within 3-day grace period
      pending/late → missed  if draw_date passed more than 3 days ago
    """
    db = next(get_db())
    try:
        now           = _utcnow()
        late_cutoff   = now - timedelta(days=0)
        missed_cutoff = now - timedelta(days=3)

        # Collect IDs first, then do atomic bulk UPDATE so a cashier marking a
        # payment "paid" between our read and our commit doesn't get overwritten.
        late_week_ids = [r[0] for r in (
            db.query(Week.id)
            .filter(Week.draw_date < late_cutoff, Week.draw_date >= missed_cutoff)
            .all()
        )]
        missed_week_ids = [r[0] for r in (
            db.query(Week.id).filter(Week.draw_date < missed_cutoff).all()
        )]

        late_count = 0
        if late_week_ids:
            late_count = db.query(Payment).filter(
                Payment.status == "pending",
                Payment.week_id.in_(late_week_ids),
            ).update({"status": "late"}, synchronize_session=False)

        # Fetch newly-missed IDs *before* the UPDATE so we can notify them
        newly_missed_payments = []
        if missed_week_ids:
            newly_missed_payments = (
                db.query(Payment)
                .filter(Payment.status.in_(["pending", "late"]),
                        Payment.week_id.in_(missed_week_ids))
                .all()
            )
            if newly_missed_payments:
                missed_ids = [p.id for p in newly_missed_payments]
                db.query(Payment).filter(
                    Payment.id.in_(missed_ids),
                    Payment.status.in_(["pending", "late"]),  # skip if paid in the meantime
                ).update({"status": "missed"}, synchronize_session=False)

        if late_count or newly_missed_payments:
            db.commit()
            from routers.notifications import send_missed_payment
            for p in newly_missed_payments:
                send_missed_payment(p, db)
            logger.info("scheduler: %d → late, %d → missed", late_count, len(newly_missed_payments))
    except Exception as e:
        logger.error("scheduler auto-close error: %s", e)
        db.rollback()
    finally:
        db.close()


async def send_pre_draw_reminders():
    """
    Daily at 18:00 UTC (9 PM EAT): SMS members with unpaid weeks whose next draw
    is within 48 hours.
    """
    db = next(get_db())
    try:
        from routers.notifications import send_missed_payment as send_payment_reminder
        now    = _utcnow()
        cutoff = now + timedelta(hours=48)
        upcoming_weeks = (
            db.query(Week)
            .filter(Week.status == "pending", Week.draw_date >= now, Week.draw_date <= cutoff)
            .all()
        )
        sent = 0
        for w in upcoming_weeks:
            unpaid = (
                db.query(Payment)
                .filter(Payment.week_id == w.id, Payment.status.in_(["pending", "late"]))
                .all()
            )
            for p in unpaid:
                try:
                    send_payment_reminder(p, db)
                    sent += 1
                except Exception:
                    pass
        if sent:
            logger.info("scheduler: pre-draw reminders sent: %d", sent)
    except Exception as e:
        logger.error("scheduler pre-draw reminder error: %s", e)
    finally:
        db.close()

async def run_daily_backup():
    """Daily at 02:00 UTC: pg_dump → /tmp/equb_backups/, keep last 7."""
    import subprocess
    from urllib.parse import urlparse
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        logger.warning("backup: DATABASE_URL not set")
        return
    try:
        p = urlparse(db_url)
        backup_dir = "/tmp/equb_backups"
        os.makedirs(backup_dir, exist_ok=True)
        timestamp = _utcnow().strftime("%Y%m%d_%H%M%S")
        out_file = os.path.join(backup_dir, f"equb_{timestamp}.sql")
        env = os.environ.copy()
        env["PGPASSWORD"] = p.password or ""
        cmd = [
            "pg_dump",
            "-h", p.hostname or "localhost",
            "-p", str(p.port or 5432),
            "-U", p.username or "",
            "-d", p.path.lstrip("/"),
            "--no-password", "--format=plain", "--encoding=UTF8",
            "-f", out_file,
        ]
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
        # Always prune old backups (including failed zero-byte files) so disk doesn't fill
        all_bk = sorted(
            [f for f in os.listdir(backup_dir) if f.startswith("equb_") and f.endswith(".sql")],
            reverse=True,
        )
        for old in all_bk[7:]:
            try:
                os.remove(os.path.join(backup_dir, old))
            except OSError:
                pass
        if r.returncode != 0:
            logger.error("backup: pg_dump failed: %s", r.stderr[:300])
            return
        size = os.path.getsize(out_file)
        logger.info("backup: saved %s (%d bytes)", out_file, size)
    except FileNotFoundError:
        logger.warning("backup: pg_dump not found on this host")
    except Exception as e:
        logger.error("backup: error: %s", e)


async def send_weekly_report():
    """Monday 08:00 UTC: email weekly summary to admin."""
    db = next(get_db())
    try:
        from database import Settings, NotificationSettings, Cycle, Week, Member, Payment
        cfg = db.query(NotificationSettings).first()
        if not cfg or not cfg.smtp_host or not cfg.smtp_user or not cfg.smtp_password:
            return
        gs = db.query(Settings).first()
        admin_email = cfg.email_from or cfg.smtp_user
        group_name  = (gs.group_name if gs and gs.group_name else None) or "Equb"
        now         = _utcnow()
        week_ago    = now - timedelta(days=7)

        active_cycles = db.query(Cycle).filter(Cycle.status == "active").all()
        sections = []
        for cycle in active_cycles:
            week_ids = [r[0] for r in db.query(Week.id).filter(Week.cycle_id == cycle.id).all()]
            paid_wk  = db.query(Payment).filter(
                Payment.week_id.in_(week_ids),
                Payment.status == "paid",
                Payment.paid_date >= week_ago,
            ).count() if week_ids else 0
            outstanding = db.query(Payment).filter(
                Payment.week_id.in_(week_ids),
                Payment.status.in_(["late", "missed"]),
            ).count() if week_ids else 0
            members_ct = db.query(Member).filter(Member.status == "active").count()
            sections.append(
                f"Cycle: {cycle.name}\n"
                f"  - Payments collected this week : {paid_wk}\n"
                f"  - Outstanding (late/missed)    : {outstanding}\n"
                f"  - Active members               : {members_ct}\n"
            )

        body = (
            f"Weekly Report — {now.strftime('%d %B %Y')}\n"
            f"{'='*40}\n\n"
            + ("\n".join(sections) if sections else "No active cycles this week.\n")
            + "\n\nThis report is sent every Monday automatically by your Equb system."
        )
        from routers.notifications import _send_email
        status, detail = _send_email(admin_email, f"{group_name} — Weekly Report", body, cfg)
        if status == "sent":
            logger.info("weekly report: sent to %s", admin_email)
        else:
            logger.warning("weekly report: %s", detail)
    except Exception as e:
        logger.error("weekly report error: %s", e)
    finally:
        db.close()


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler = None
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger
        from routers.notifications import fire_scheduled_notifications

        scheduler = AsyncIOScheduler()
        scheduler.add_job(auto_close_past_weeks,        CronTrigger(hour=21, minute=0))
        scheduler.add_job(send_pre_draw_reminders,      CronTrigger(hour=18, minute=0))
        scheduler.add_job(fire_scheduled_notifications, IntervalTrigger(minutes=5))
        scheduler.add_job(run_daily_backup,             CronTrigger(hour=2, minute=0))
        scheduler.add_job(send_weekly_report,           CronTrigger(day_of_week="mon", hour=8, minute=0))
        scheduler.start()
        logger.info("scheduler: jobs registered — auto-close 21:00, reminders 18:00, backup 02:00, weekly-report Mon 08:00")
    except ImportError:
        logger.warning("scheduler: APScheduler not installed — skipping scheduled jobs")
    yield
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Equb Management System",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(members.router,              prefix="/api/members",       tags=["members"])
app.include_router(draws.router,                prefix="/api/draws",         tags=["draws"])
app.include_router(payments.router,             prefix="/api/payments",      tags=["payments"])
app.include_router(reports.router,              prefix="/api/reports",       tags=["reports"])
app.include_router(notifications.router,        prefix="/api/notifications", tags=["notifications"])
app.include_router(auth_router.router,          prefix="/api/auth",          tags=["auth"])
app.include_router(settings_router.router,      prefix="/api/settings",      tags=["settings"])
app.include_router(disbursements_router.router, prefix="/api/disbursements", tags=["disbursements"])
app.include_router(sms_gateway_router.router,   prefix="/api/sms-gateway",   tags=["sms-gateway"])
app.include_router(debts_router.router,         prefix="/api/debts",         tags=["debts"])
app.include_router(marketplace_router.router,   prefix="/api/marketplace",   tags=["marketplace"])
app.include_router(portal_router.router,        prefix="/api/portal",        tags=["portal"])
app.include_router(backup_router.router,        prefix="/api/backup",        tags=["backup"])

# ── Middleware ────────────────────────────────────────────────────────────────
# NOTE: Starlette processes @app.middleware decorators bottom-up (last registered = first to run).
# SessionMiddleware (add_middleware) runs before all @app.middleware decorators.

_GATEWAY_PUBLIC = {"/api/sms-gateway/pending", "/api/sms-gateway/ack"}
_PUBLIC         = {"/login", "/favicon.ico", "/portal", "/health"}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"]   = "nosniff"
    response.headers["X-Frame-Options"]           = "DENY"
    response.headers["X-XSS-Protection"]          = "1; mode=block"
    response.headers["Referrer-Policy"]           = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"]        = "camera=(), microphone=(), geolocation=()"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' unpkg.com cdn.jsdelivr.net cdn.tailwindcss.com; "
        "style-src 'self' 'unsafe-inline' fonts.googleapis.com cdn.tailwindcss.com; "
        "font-src 'self' fonts.gstatic.com data:; "
        "img-src 'self' data: blob: *; "
        "connect-src 'self'; "
        "frame-ancestors 'none';"
    )
    response.headers["Content-Security-Policy"] = csp
    return response


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if (path in _PUBLIC or path in _GATEWAY_PUBLIC
            or path.startswith("/static") or path.startswith("/api/portal")):
        return await call_next(request)

    uid   = request.session.get("user_id")
    token = request.session.get("_token")
    if not uid or (token and _invalidated_sessions.get(token, 0) > time.time()):
        request.session.clear()
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        return RedirectResponse("/login", status_code=302)

    request.state.user_id   = uid
    request.state.user_role = request.session.get("user_role", "cashier")
    request.state.user_name = request.session.get("user_name", "User")
    return await call_next(request)


@app.middleware("http")
async def csrf_middleware(request: Request, call_next):
    if (request.url.path.startswith("/api/")
            and request.url.path not in _GATEWAY_PUBLIC
            and request.method in ("POST", "PUT", "DELETE", "PATCH")):
        content_type = request.headers.get("content-type", "")
        if not content_type.startswith("multipart/form-data"):
            if request.headers.get("X-Requested-With") != "XMLHttpRequest":
                return JSONResponse({"detail": "CSRF check failed"}, status_code=403)
    return await call_next(request)


app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    max_age=3600,
    https_only=IS_PRODUCTION,
    same_site="lax",
)

# ── Template context ──────────────────────────────────────────────────────────
def _ctx(request: Request) -> dict:
    db: Session = next(get_db())
    try:
        s    = db.query(Settings).first()
        role = getattr(request.state, "user_role", "cashier")
        perms = _get_permissions(db)
        if role == "superadmin":
            effective = {feat: True for feat in (
                "manage_members", "run_draws", "disbursements",
                "view_reports", "manage_users", "notifications",
            )}
        else:
            effective = perms.get(role, {})
        return {
            "user_name":     getattr(request.state, "user_name", ""),
            "user_role":     role,
            "permissions":   effective,
            "group_name":    s.group_name    if s else "እቁብ",
            "group_tagline": s.group_tagline if s else "Equb Manager",
            "logo_url":      s.logo_url      if s else None,
        }
    finally:
        db.close()

# ── Auth routes ───────────────────────────────────────────────────────────────
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"error": None, "locked": False})


@app.post("/login")
async def login_submit(request: Request,
                       username: str = Form(...),
                       password: str = Form(...)):
    ip = _get_client_ip(request)
    if _is_rate_limited(ip):
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "Too many failed attempts. Please wait 10 minutes before trying again.", "locked": True},
            status_code=429,
        )

    db: Session = next(get_db())
    try:
        user = db.query(User).filter(User.username == username, User.is_active).first()
        if not user or not _pwd.verify(password, user.password_hash):
            _record_attempt(ip)
            remaining = max(0, _RATE_MAX - len(_login_attempts[ip]))
            msg = "Invalid username or password."
            if remaining <= 2:
                msg += f" {remaining} attempt(s) remaining before lockout."
            log_action(db, user=None, action="login_failed", table="users",
                       description=f"Failed login for '{username}' from {ip}")
            db.commit()
            return templates.TemplateResponse(
                request, "login.html",
                {"error": msg, "locked": False},
                status_code=401,
            )
        _clear_attempts(ip)
        request.session.clear()
        if getattr(user, "totp_enabled", False) and getattr(user, "totp_secret", None):
            request.session["pending_2fa_user_id"] = user.id
            request.session["pending_2fa_role"]    = user.role
            request.session["pending_2fa_name"]    = user.full_name
            log_action(db, user=user, action="login", table="users", record_id=user.id,
                       description=f"{user.username} passed password check, awaiting 2FA from {ip}")
        else:
            token = secrets.token_hex(16)
            request.session["_token"]    = token
            request.session["user_id"]   = user.id
            request.session["user_role"] = user.role
            request.session["user_name"] = user.full_name
            log_action(db, user=user, action="login", table="users", record_id=user.id,
                       description=f"{user.username} logged in from {ip}")
        db.commit()
    finally:
        db.close()

    if request.session.get("pending_2fa_user_id"):
        return RedirectResponse("/login/2fa", status_code=302)
    return RedirectResponse("/", status_code=302)


@app.get("/login/2fa", response_class=HTMLResponse)
async def totp_page(request: Request):
    if not request.session.get("pending_2fa_user_id"):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"step": "totp", "error": None})


@app.post("/login/2fa")
async def totp_submit(request: Request, totp_code: str = Form(...)):
    uid = request.session.get("pending_2fa_user_id")
    if not uid:
        return RedirectResponse("/login", status_code=302)
    db: Session = next(get_db())
    try:
        user = db.query(User).filter(User.id == uid).first()
        if not user:
            request.session.clear()
            return RedirectResponse("/login", status_code=302)
        try:
            import pyotp
            valid = pyotp.TOTP(user.totp_secret).verify(totp_code.strip(), valid_window=1)
        except Exception:
            valid = False
        if not valid:
            return templates.TemplateResponse(
                request, "login.html",
                {"step": "totp", "error": "Invalid code. Please try again."},
                status_code=401,
            )
        role  = request.session.pop("pending_2fa_role", user.role)
        name  = request.session.pop("pending_2fa_name", user.full_name)
        request.session.pop("pending_2fa_user_id", None)
        token = secrets.token_hex(16)
        request.session["_token"]    = token
        request.session["user_id"]   = user.id
        request.session["user_role"] = role
        request.session["user_name"] = name
        ip = _get_client_ip(request)
        log_action(db, user=user, action="login", table="users", record_id=user.id,
                   description=f"{user.username} completed 2FA login from {ip}")
        db.commit()
    finally:
        db.close()
    return RedirectResponse("/", status_code=302)


@app.get("/logout")
async def logout(request: Request):
    token = request.session.get("_token")
    uid   = request.session.get("user_id")
    uname = request.session.get("user_name", "")
    if token:
        _invalidate_session(token)
    request.session.clear()
    if uid:
        db: Session = next(get_db())
        try:
            user = db.query(User).filter(User.id == uid).first()
            log_action(db, user=user, action="logout", table="users", record_id=uid,
                       description=f"{uname} logged out")
            db.commit()
        finally:
            db.close()
    return RedirectResponse("/login", status_code=302)

# ── Admin API ─────────────────────────────────────────────────────────────────
@app.post("/api/admin/auto-close-weeks")
async def manual_auto_close(request: Request):
    if getattr(request.state, "user_role", "") not in ("admin", "superadmin"):
        return JSONResponse({"detail": "Admin only"}, status_code=403)
    await auto_close_past_weeks()
    return {"ok": True, "message": "Past pending payments marked as missed"}

# ── Page routes ───────────────────────────────────────────────────────────────
@app.get("/",              response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", _ctx(request))

@app.get("/members",       response_class=HTMLResponse)
async def members_page(request: Request):
    return templates.TemplateResponse(request, "members.html", _ctx(request))

@app.get("/draws",         response_class=HTMLResponse)
async def draws_page(request: Request):
    return templates.TemplateResponse(request, "draws.html", _ctx(request))

@app.get("/payments",      response_class=HTMLResponse)
async def payments_page(request: Request):
    return templates.TemplateResponse(request, "payments.html", _ctx(request))

@app.get("/reports",       response_class=HTMLResponse)
async def reports_page(request: Request):
    ctx = _ctx(request)
    if not ctx["permissions"].get("view_reports"):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "reports.html", ctx)

@app.get("/notifications",  response_class=HTMLResponse)
async def notifications_page(request: Request):
    ctx = _ctx(request)
    if not ctx["permissions"].get("notifications"):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "notifications.html", ctx)

@app.get("/settings",      response_class=HTMLResponse)
async def settings_page(request: Request):
    if getattr(request.state, "user_role", "") not in ("admin", "superadmin"):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "settings.html", _ctx(request))

@app.get("/api-docs",      response_class=HTMLResponse)
async def api_docs_page(request: Request):
    if getattr(request.state, "user_role", "") != "superadmin":
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "api_docs.html", _ctx(request))

@app.get("/openapi.json", include_in_schema=False)
async def openapi_spec(request: Request):
    if getattr(request.state, "user_role", "") != "superadmin":
        raise HTTPException(status_code=403, detail="Not authorized")
    return app.openapi()

@app.get("/collections",   response_class=HTMLResponse)
async def collections_page(request: Request):
    return templates.TemplateResponse(request, "debts.html", _ctx(request))

@app.get("/marketplace",   response_class=HTMLResponse)
async def marketplace_page(request: Request):
    return templates.TemplateResponse(request, "marketplace.html", _ctx(request))

@app.get("/portal",        response_class=HTMLResponse)
async def portal_page(request: Request):
    return templates.TemplateResponse(request, "portal.html", {"request": request})

# ── One-time setup (permanently disabled once any user exists) ────────────────
@app.get("/setup-admin-equb2024", response_class=HTMLResponse)
async def setup_admin(token: str = ""):
    if os.environ.get("SETUP_DISABLED", "").lower() in ("1", "true", "yes"):
        return HTMLResponse("<h2>Setup endpoint is disabled</h2>", status_code=403)
    expected = os.environ.get("SETUP_TOKEN", "equb-init-7x9k")
    if not token or token != expected:
        return HTMLResponse("<h2>Invalid token</h2>", status_code=403)
    db: Session = next(get_db())
    try:
        if db.query(User).first():
            return HTMLResponse("<h2>Setup already complete. Login at /login</h2>", status_code=403)
        pw = secrets.token_urlsafe(16)
        u  = User(
            username="admin",
            full_name="Administrator",
            role="superadmin",
            is_active=True,
            password_hash=_pwd.hash(pw),
        )
        db.add(u)
        db.commit()
        logger.warning("SETUP: Admin created — username: admin / password: %s", pw)
        return HTMLResponse(
            "<h2>Admin created!</h2>"
            "<p>Credentials printed to server logs. Check Railway logs for the temporary password.</p>"
            "<p><a href='/login'>Go to login</a> and change your password immediately.</p>"
            "<p><strong>Set SETUP_DISABLED=true in your environment to disable this endpoint.</strong></p>"
        )
    finally:
        db.close()

# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok"}

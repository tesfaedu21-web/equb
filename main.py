import logging
import os
import time
import secrets
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request, Form
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from database import init_db, get_db, User, Settings, Payment, Week, _pwd
from routers.deps import _get_permissions
from sqlalchemy.orm import Session
from routers import members, draws, payments, reports, notifications
from routers import auth as auth_router
from routers import settings as settings_router
from routers import disbursements as disbursements_router
from routers import sms_gateway as sms_gateway_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("equb")

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

def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    return forwarded.split(",")[0].strip() if forwarded else (request.client.host or "unknown")

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
_invalidated_sessions: set = set()   # set of invalidated session tokens

def _invalidate_session(token: str):
    _invalidated_sessions.add(token)
    # Prune expired tokens (older than session max_age=3600s + buffer)
    # We store (token, timestamp) — use a simple size cap for memory safety
    if len(_invalidated_sessions) > 10000:
        _invalidated_sessions.clear()

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Equb Management System",
    docs_url=None if IS_PRODUCTION else "/api/docs",
    redoc_url=None if IS_PRODUCTION else "/api/redoc",
    openapi_url=None if IS_PRODUCTION else "/openapi.json",
)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

app.include_router(members.router,              prefix="/api/members",       tags=["members"])
app.include_router(draws.router,                prefix="/api/draws",         tags=["draws"])
app.include_router(payments.router,             prefix="/api/payments",      tags=["payments"])
app.include_router(reports.router,              prefix="/api/reports",       tags=["reports"])
app.include_router(notifications.router,        prefix="/api/notifications", tags=["notifications"])
app.include_router(auth_router.router,          prefix="/api/auth",          tags=["auth"])
app.include_router(settings_router.router,      prefix="/api/settings",      tags=["settings"])
app.include_router(disbursements_router.router, prefix="/api/disbursements", tags=["disbursements"])
app.include_router(sms_gateway_router.router,   prefix="/api/sms-gateway",   tags=["sms-gateway"])


# ── Nightly scheduler jobs ────────────────────────────────────────────────────
def _utcnow():
    """Naive UTC datetime — compatible with naive datetimes stored in the DB."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def auto_close_past_weeks():
    """
    Nightly at 21:00 UTC (midnight EAT):
      pending  → late    if draw_date passed but within 3 days (grace period)
      late     → missed  if draw_date passed more than 3 days ago
      pending  → missed  if draw_date passed more than 3 days ago (caught late)
    """
    db = next(get_db())
    try:
        now = _utcnow()
        late_cutoff   = now - timedelta(days=0)   # draw_date < now → at least late
        missed_cutoff = now - timedelta(days=3)   # draw_date < now-3d → missed

        # pending → late  (draw_date passed, within 3-day grace window)
        newly_late = (
            db.query(Payment)
            .join(Week, Week.id == Payment.week_id)
            .filter(Payment.status == "pending",
                    Week.draw_date < late_cutoff,
                    Week.draw_date >= missed_cutoff)
            .all()
        )
        for p in newly_late:
            p.status = "late"

        # pending or late → missed  (3+ days past draw_date)
        newly_missed = (
            db.query(Payment)
            .join(Week, Week.id == Payment.week_id)
            .filter(Payment.status.in_(["pending", "late"]),
                    Week.draw_date < missed_cutoff)
            .all()
        )
        for p in newly_missed:
            p.status = "missed"

        if newly_late or newly_missed:
            db.commit()
            # Send SMS to newly missed members
            from routers.notifications import send_missed_payment
            for p in newly_missed:
                send_missed_payment(p, db)
            logger.info("scheduler: %d → late, %d → missed", len(newly_late), len(newly_missed))
    except Exception as e:
        logger.error("scheduler auto-close error: %s", e)
        db.rollback()
    finally:
        db.close()


async def send_pre_draw_reminders():
    """
    Daily at 18:00 UTC (9 PM EAT): SMS any member with unpaid weeks whose next draw
    is within 48 hours. Helps members pay before the draw so they remain eligible.
    """
    db = next(get_db())
    try:
        from routers.notifications import send_payment_reminder
        now = _utcnow()
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler = None
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
        scheduler = AsyncIOScheduler()
        scheduler.add_job(auto_close_past_weeks,   CronTrigger(hour=21, minute=0))
        scheduler.add_job(send_pre_draw_reminders, CronTrigger(hour=18, minute=0))
        scheduler.start()
        logger.info("scheduler: nightly auto-close scheduled (21:00 UTC)")
        logger.info("scheduler: pre-draw reminders scheduled (18:00 UTC)")
    except ImportError:
        logger.warning("scheduler: APScheduler not installed — skipping scheduled jobs")
    yield
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)


# Wire lifespan after definition (app is created above; lifespan references scheduler functions below it)
app.router.lifespan_context = lifespan


# ── Security headers ──────────────────────────────────────────────────────────
@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"]  = "nosniff"
    response.headers["X-Frame-Options"]          = "DENY"
    response.headers["X-XSS-Protection"]         = "1; mode=block"
    response.headers["Referrer-Policy"]          = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"]       = "camera=(), microphone=(), geolocation=()"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # CSP: allow CDN resources needed by the app
    csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' unpkg.com cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' fonts.googleapis.com; "
        "font-src 'self' fonts.gstatic.com data:; "
        "img-src 'self' data: blob: *; "
        "connect-src 'self'; "
        "frame-ancestors 'none';"
    )
    response.headers["Content-Security-Policy"] = csp
    return response


# ── Auth middleware ───────────────────────────────────────────────────────────
# Android SMS gateway endpoints use device-token auth, not session auth
_GATEWAY_PUBLIC = {"/api/sms-gateway/pending", "/api/sms-gateway/ack"}
_PUBLIC = {"/login", "/favicon.ico"}

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in _PUBLIC or path in _GATEWAY_PUBLIC or path.startswith("/static"):
        return await call_next(request)

    uid = request.session.get("user_id")
    token = request.session.get("_token")
    if not uid or (token and token in _invalidated_sessions):
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
            and request.url.path not in _GATEWAY_PUBLIC  # gateway uses device-token auth
            and request.method in ("POST", "PUT", "DELETE", "PATCH")):
        content_type = request.headers.get("content-type", "")
        # Multipart file uploads can't originate cross-site (browser file API restriction)
        if not content_type.startswith("multipart/form-data"):
            if request.headers.get("X-Requested-With") != "XMLHttpRequest":
                return JSONResponse({"detail": "CSRF check failed"}, status_code=403)
    return await call_next(request)


app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    max_age=3600,           # 1-hour session
    https_only=IS_PRODUCTION,
    same_site="lax",
)


# ── Template context helper ───────────────────────────────────────────────────
def _ctx(request: Request) -> dict:
    db: Session = next(get_db())
    try:
        s = db.query(Settings).first()
        role = getattr(request.state, "user_role", "cashier")
        perms = _get_permissions(db)
        # Effective permissions for this user's role (superadmin gets everything)
        if role == "superadmin":
            effective = {feat: True for feat in ("manage_members", "run_draws", "disbursements", "view_reports", "manage_users", "notifications")}
        else:
            effective = perms.get(role, {})
        return {
            "user_name":    getattr(request.state, "user_name", ""),
            "user_role":    role,
            "permissions":  effective,
            "group_name":   s.group_name    if s else "እቁብ",
            "group_tagline":s.group_tagline if s else "Equb Manager",
            "logo_url":     s.logo_url      if s else None,
        }
    finally:
        db.close()


# ── Login / Logout ────────────────────────────────────────────────────────────
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

    # Rate-limit check
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
            return templates.TemplateResponse(
                request, "login.html",
                {"error": msg, "locked": False},
                status_code=401,
            )
        # Successful password check — now check 2FA
        _clear_attempts(ip)
        request.session.clear()
        if getattr(user, "totp_enabled", False) and getattr(user, "totp_secret", None):
            # Store pending state; full session granted after TOTP verify
            request.session["pending_2fa_user_id"] = user.id
            request.session["pending_2fa_role"]    = user.role
            request.session["pending_2fa_name"]    = user.full_name
        else:
            token = secrets.token_hex(16)
            request.session["_token"]    = token
            request.session["user_id"]   = user.id
            request.session["user_role"] = user.role
            request.session["user_name"] = user.full_name
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
            totp = pyotp.TOTP(user.totp_secret)
            valid = totp.verify(totp_code.strip(), valid_window=1)
        except Exception:
            valid = False
        if not valid:
            return templates.TemplateResponse(
                request, "login.html",
                {"step": "totp", "error": "Invalid code. Please try again."},
                status_code=401,
            )
        # TOTP verified — promote to full session
        role = request.session.pop("pending_2fa_role", user.role)
        name = request.session.pop("pending_2fa_name", user.full_name)
        request.session.pop("pending_2fa_user_id", None)
        token = secrets.token_hex(16)
        request.session["_token"]    = token
        request.session["user_id"]   = user.id
        request.session["user_role"] = role
        request.session["user_name"] = name
    finally:
        db.close()
    return RedirectResponse("/", status_code=302)


@app.get("/logout")
async def logout(request: Request):
    token = request.session.get("_token")
    if token:
        _invalidate_session(token)
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


# ── Manual trigger for auto-close (admin only) ────────────────────────────────
@app.post("/api/admin/auto-close-weeks")
async def manual_auto_close(request: Request):
    if getattr(request.state, "user_role", "") not in ("admin", "superadmin"):
        return JSONResponse({"detail": "Admin only"}, status_code=403)
    await auto_close_past_weeks()
    return {"ok": True, "message": "Past pending payments marked as missed"}


# ── Page routes ───────────────────────────────────────────────────────────────
@app.get("/",            response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", _ctx(request))

@app.get("/members",     response_class=HTMLResponse)
async def members_page(request: Request):
    return templates.TemplateResponse(request, "members.html", _ctx(request))

@app.get("/draws",       response_class=HTMLResponse)
async def draws_page(request: Request):
    return templates.TemplateResponse(request, "draws.html", _ctx(request))

@app.get("/payments",    response_class=HTMLResponse)
async def payments_page(request: Request):
    return templates.TemplateResponse(request, "payments.html", _ctx(request))

@app.get("/reports",     response_class=HTMLResponse)
async def reports_page(request: Request):
    ctx = _ctx(request)
    if not ctx["permissions"].get("view_reports"):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "reports.html", ctx)

@app.get("/notifications", response_class=HTMLResponse)
async def notifications_page(request: Request):
    ctx = _ctx(request)
    if not ctx["permissions"].get("notifications"):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "notifications.html", ctx)

@app.get("/settings",    response_class=HTMLResponse)
async def settings_page(request: Request):
    if getattr(request.state, "user_role", "") not in ("admin", "superadmin"):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "settings.html", _ctx(request))




# ── ONE-TIME SETUP (permanently disabled once any user exists) ────────────────
@app.get("/setup-admin-equb2024", response_class=HTMLResponse)
async def setup_admin(token: str = ""):
    if os.environ.get("SETUP_DISABLED", "").lower() in ("1", "true", "yes"):
        return HTMLResponse("<h2>Setup endpoint is disabled</h2>", status_code=403)
    expected = os.environ.get("SETUP_TOKEN", "equb-init-7x9k")
    if not token or token != expected:
        return HTMLResponse("<h2>Invalid token</h2>", status_code=403)
    db: Session = next(get_db())
    try:
        # Disabled permanently once ANY user exists in the DB
        existing = db.query(User).first()
        if existing:
            return HTMLResponse("<h2>Setup already complete. Login at /login</h2>", status_code=403)
        pw = secrets.token_urlsafe(16)
        u = User(
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

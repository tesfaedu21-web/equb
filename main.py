import os
import time
from collections import defaultdict
from datetime import datetime

from fastapi import FastAPI, Request, Form
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from database import init_db, get_db, User, Settings, Payment, Week, _pwd
from sqlalchemy.orm import Session
from routers import members, draws, payments, reports, notifications
from routers import auth as auth_router
from routers import settings as settings_router
from routers import disbursements as disbursements_router

SECRET_KEY = os.environ.get("SECRET_KEY", "equb-secret-change-in-production-2024")
IS_PRODUCTION = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("DATABASE_URL", "").startswith("postgresql"))

# ── Rate limiting (in-memory, per IP) ────────────────────────────────────────
_login_attempts: dict = defaultdict(list)   # ip -> [unix timestamps]
_RATE_WINDOW   = 300   # 5-minute window
_RATE_MAX      = 5     # max attempts per window
_LOCKOUT_SEC   = 600   # 10-minute lockout after exceeding

def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    return forwarded.split(",")[0].strip() if forwarded else (request.client.host or "unknown")

def _is_rate_limited(ip: str) -> bool:
    now = time.time()
    attempts = [t for t in _login_attempts[ip] if now - t < _RATE_WINDOW]
    _login_attempts[ip] = attempts
    return len(attempts) >= _RATE_MAX

def _record_attempt(ip: str):
    _login_attempts[ip].append(time.time())

def _clear_attempts(ip: str):
    _login_attempts.pop(ip, None)

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


# ── Nightly auto-close job ────────────────────────────────────────────────────
async def auto_close_past_weeks():
    """Mark all pending payments for past draw-date weeks as missed."""
    db = next(get_db())
    try:
        now = datetime.utcnow()
        past_pending = (
            db.query(Payment)
            .join(Week, Week.id == Payment.week_id)
            .filter(Payment.status == "pending", Week.draw_date < now)
            .all()
        )
        for p in past_pending:
            p.status = "missed"
        if past_pending:
            db.commit()
            print(f"[scheduler] Auto-closed {len(past_pending)} pending → missed")
    except Exception as e:
        print(f"[scheduler] Error: {e}")
        db.rollback()
    finally:
        db.close()


@app.on_event("startup")
async def startup():
    init_db()
    # Schedule nightly auto-close at 21:00 UTC (midnight Addis Ababa UTC+3)
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
        scheduler = AsyncIOScheduler()
        scheduler.add_job(auto_close_past_weeks, CronTrigger(hour=21, minute=0))
        scheduler.start()
        print("[scheduler] Nightly auto-close job scheduled (21:00 UTC = midnight EAT)")
    except ImportError:
        print("[scheduler] APScheduler not installed — skipping auto-close job")


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
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' cdn.tailwindcss.com unpkg.com; "
        "style-src 'self' 'unsafe-inline' fonts.googleapis.com; "
        "font-src 'self' fonts.gstatic.com data:; "
        "img-src 'self' data: blob: *; "
        "connect-src 'self'; "
        "frame-ancestors 'none';"
    )
    response.headers["Content-Security-Policy"] = csp
    return response


# ── Auth middleware ───────────────────────────────────────────────────────────
_PUBLIC = {"/login", "/favicon.ico"}

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in _PUBLIC or path.startswith("/static"):
        return await call_next(request)

    uid = request.session.get("user_id")
    if not uid:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        return RedirectResponse("/login", status_code=302)

    request.state.user_id   = uid
    request.state.user_role = request.session.get("user_role", "cashier")
    request.state.user_name = request.session.get("user_name", "User")
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
        return {
            "user_name":    getattr(request.state, "user_name", ""),
            "user_role":    getattr(request.state, "user_role", ""),
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
        # Successful login
        _clear_attempts(ip)
        request.session.clear()                  # regenerate session on login
        request.session["user_id"]   = user.id
        request.session["user_role"] = user.role
        request.session["user_name"] = user.full_name
    finally:
        db.close()
    return RedirectResponse("/", status_code=302)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


# ── Manual trigger for auto-close (admin only) ────────────────────────────────
@app.post("/api/admin/auto-close-weeks")
async def manual_auto_close(request: Request):
    if getattr(request.state, "user_role", "") != "admin":
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
    return templates.TemplateResponse(request, "reports.html", _ctx(request))

@app.get("/notifications", response_class=HTMLResponse)
async def notifications_page(request: Request):
    if getattr(request.state, "user_role", "") != "admin":
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "notifications.html", _ctx(request))

@app.get("/settings",    response_class=HTMLResponse)
async def settings_page(request: Request):
    if getattr(request.state, "user_role", "") != "admin":
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "settings.html", _ctx(request))

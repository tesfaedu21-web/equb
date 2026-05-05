import os
from fastapi import FastAPI, Request, Form
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from database import init_db, get_db, User, Settings, _pwd
from sqlalchemy.orm import Session
from routers import members, draws, payments, reports, notifications
from routers import auth as auth_router
from routers import settings as settings_router
from routers import disbursements as disbursements_router

SECRET_KEY = os.environ.get("SECRET_KEY", "equb-secret-change-in-production-2024")

app = FastAPI(title="Equb Management System", docs_url="/api/docs")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

app.include_router(members.router,       prefix="/api/members",       tags=["members"])
app.include_router(draws.router,         prefix="/api/draws",         tags=["draws"])
app.include_router(payments.router,      prefix="/api/payments",      tags=["payments"])
app.include_router(reports.router,       prefix="/api/reports",       tags=["reports"])
app.include_router(notifications.router, prefix="/api/notifications", tags=["notifications"])
app.include_router(auth_router.router,   prefix="/api/auth",          tags=["auth"])
app.include_router(settings_router.router, prefix="/api/settings",       tags=["settings"])
app.include_router(disbursements_router.router, prefix="/api/disbursements", tags=["disbursements"])


@app.on_event("startup")
async def startup():
    init_db()


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


# ── Auth middleware (must be registered BEFORE SessionMiddleware is added) ────

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


# SessionMiddleware must be added AFTER @app.middleware("http") so it wraps
# the outer layer and populates request.session before auth runs.
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=1800)  # 30 minutes


# ── Helper: template context ──────────────────────────────────────────────────

def _ctx(request: Request) -> dict:
    db: Session = next(get_db())
    try:
        s = db.query(Settings).first()
        return {
            "user_name": getattr(request.state, "user_name", ""),
            "user_role": getattr(request.state, "user_role", ""),
            "group_name": s.group_name if s else "እቁብ",
            "group_tagline": s.group_tagline if s else "Equb Manager",
            "logo_url": s.logo_url if s else None,
        }
    finally:
        db.close()


# ── Login / Logout ────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
async def login_submit(request: Request,
                       username: str = Form(...),
                       password: str = Form(...)):
    db: Session = next(get_db())
    try:
        user = db.query(User).filter(User.username == username,
                                     User.is_active == True).first()
        if not user or not _pwd.verify(password, user.password_hash):
            return templates.TemplateResponse(
                request, "login.html",
                {"error": "Invalid username or password"},
                status_code=401,
            )
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


# ── Page routes ───────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", _ctx(request))


@app.get("/members", response_class=HTMLResponse)
async def members_page(request: Request):
    return templates.TemplateResponse(request, "members.html", _ctx(request))


@app.get("/draws", response_class=HTMLResponse)
async def draws_page(request: Request):
    return templates.TemplateResponse(request, "draws.html", _ctx(request))


@app.get("/payments", response_class=HTMLResponse)
async def payments_page(request: Request):
    return templates.TemplateResponse(request, "payments.html", _ctx(request))


@app.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request):
    return templates.TemplateResponse(request, "reports.html", _ctx(request))


@app.get("/notifications", response_class=HTMLResponse)
async def notifications_page(request: Request):
    if getattr(request.state, "user_role", "") != "admin":
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "notifications.html", _ctx(request))


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    if getattr(request.state, "user_role", "") != "admin":
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "settings.html", _ctx(request))

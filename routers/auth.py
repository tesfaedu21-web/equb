import time
from collections import defaultdict
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from typing import Optional
from database import (get_db, User, Settings, _pwd, Cycle, Spot, Member, MemberSpot,
                       Week, PaymentBatch, Payment, PotTransaction,
                       PotDisbursement, AssociationExpense, DistributionCheque,
                       VoucherReturn, NotificationLog, NotificationSettings, NotificationTemplate,
                       SpotListing, DebtContact, DebtCase, AuditLog, SmsQueue, ScheduledNotification)
from routers.deps import _require_admin, _require_superadmin, _require_feature, _get_permissions, DEFAULT_PERMISSIONS

router = APIRouter()

_VALID_ROLES = {"superadmin", "admin", "cashier"}

# Per-user 2FA failure tracking: user_id → [timestamps]
_2fa_failures: dict = defaultdict(list)
_2FA_MAX = 5        # max attempts
_2FA_WINDOW = 900   # 15 minutes


def _check_2fa_rate(user_id: int):
    now = time.time()
    attempts = [t for t in _2fa_failures[user_id] if now - t < _2FA_WINDOW]
    _2fa_failures[user_id] = attempts
    if len(attempts) >= _2FA_MAX:
        raise HTTPException(status_code=429, detail="Too many 2FA attempts. Try again in 15 minutes.")


def _record_2fa_failure(user_id: int):
    _2fa_failures[user_id].append(time.time())


class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=8, max_length=128)
    full_name: str = Field(..., min_length=1, max_length=100)
    role: str = "cashier"
    email: Optional[str] = Field(default=None, max_length=254)


class UserUpdate(BaseModel):
    full_name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    role: Optional[str] = None
    is_active: Optional[bool] = None
    email: Optional[str] = Field(default=None, max_length=254)


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


class PasswordReset(BaseModel):
    new_password: str


_MIN_PASSWORD_LEN = 8


def _validate_password(pw: str):
    if len(pw) < _MIN_PASSWORD_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at least {_MIN_PASSWORD_LEN} characters long."
        )
    if pw.isdigit():
        raise HTTPException(status_code=400, detail="Password cannot be all numbers.")
    if pw.lower() in {"password","123456789","abcdefgh","equb1234","admin123","cashier1"}:
        raise HTTPException(status_code=400, detail="Password is too common. Choose a stronger password.")


def _user_dict(u: User) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "full_name": u.full_name,
        "role": u.role,
        "email": u.email,
        "is_active": u.is_active,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


@router.get("/me")
def me(request: Request, db: Session = Depends(get_db)):
    uid = getattr(request.state, "user_id", None)
    if not uid:
        raise HTTPException(status_code=401, detail="Not authenticated")
    u = db.query(User).filter(User.id == uid).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    return _user_dict(u)


@router.get("/users")
def list_users(request: Request, db: Session = Depends(get_db)):
    _require_feature(request, db, "manage_users")
    return [_user_dict(u) for u in db.query(User).order_by(User.id).all()]


@router.delete("/users/{user_id}")
def delete_user(user_id: int, request: Request, db: Session = Depends(get_db)):
    _require_superadmin(request)
    caller_id = getattr(request.state, "user_id", None)
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    if u.role == "superadmin":
        raise HTTPException(status_code=400, detail="Cannot delete the Owner account")
    if u.id == caller_id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    db.delete(u)
    db.commit()
    return {"ok": True, "deleted": u.username}


@router.post("/users")
def create_user(data: UserCreate, request: Request, db: Session = Depends(get_db)):
    caller_role = getattr(request.state, "user_role", None)
    _require_feature(request, db, "manage_users")

    if data.role not in _VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {', '.join(sorted(_VALID_ROLES))}")
    if data.role == "superadmin":
        raise HTTPException(status_code=403, detail="Cannot create another owner account.")
    if data.role == "admin" and caller_role != "superadmin":
        raise HTTPException(status_code=403, detail="Only the owner can create admin accounts.")

    if db.query(User).filter(User.username == data.username).first():
        raise HTTPException(status_code=400, detail="Username already exists")
    _validate_password(data.password)
    u = User(
        username=data.username,
        password_hash=_pwd.hash(data.password),
        full_name=data.full_name,
        role=data.role,
        email=data.email,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return _user_dict(u)


@router.put("/users/{user_id}")
def update_user(user_id: int, data: UserUpdate, request: Request, db: Session = Depends(get_db)):
    caller_role = getattr(request.state, "user_role", None)
    caller_id   = getattr(request.state, "user_id", None)
    _require_feature(request, db, "manage_users")

    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")

    # Superadmin account can only be edited by themselves
    if u.role == "superadmin" and caller_id != user_id:
        raise HTTPException(status_code=403, detail="The owner account can only be edited by the owner.")

    # Only superadmin can edit admin accounts
    if u.role == "admin" and caller_role != "superadmin":
        raise HTTPException(status_code=403, detail="Only the owner can modify admin accounts.")

    # Block assigning superadmin role
    if data.role == "superadmin":
        raise HTTPException(status_code=403, detail="Cannot assign the owner role.")

    # Only superadmin can promote someone to admin
    if data.role == "admin" and caller_role != "superadmin":
        raise HTTPException(status_code=403, detail="Only the owner can promote a user to admin.")

    for field, val in data.model_dump(exclude_none=True).items():
        setattr(u, field, val)
    db.commit()
    db.refresh(u)
    return _user_dict(u)


@router.post("/users/{user_id}/reset-password")
def reset_password(user_id: int, data: PasswordReset, request: Request, db: Session = Depends(get_db)):
    """Admin resets another user's password without needing the current one."""
    caller_role = getattr(request.state, "user_role", None)
    caller_id   = getattr(request.state, "user_id",   None)
    _require_feature(request, db, "manage_users")

    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")

    # Superadmin password can only be reset by themselves
    if u.role == "superadmin" and caller_id != user_id:
        raise HTTPException(status_code=403, detail="The owner password can only be changed by the owner.")

    # Only superadmin can reset an admin's password
    if u.role == "admin" and caller_role != "superadmin":
        raise HTTPException(status_code=403, detail="Only the owner can reset an admin password.")

    _validate_password(data.new_password)
    u.password_hash = _pwd.hash(data.new_password)
    db.commit()
    return {"ok": True, "username": u.username}


@router.post("/change-password")
def change_password(data: PasswordChange, request: Request, db: Session = Depends(get_db)):
    uid = getattr(request.state, "user_id", None)
    if not uid:
        raise HTTPException(status_code=401, detail="Not authenticated")
    u = db.query(User).filter(User.id == uid).first()
    if not u or not _pwd.verify(data.current_password, u.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    _validate_password(data.new_password)
    u.password_hash = _pwd.hash(data.new_password)
    db.commit()
    return {"ok": True}


@router.get("/permissions")
def get_permissions(request: Request, db: Session = Depends(get_db)):
    _require_superadmin(request)
    return _get_permissions(db)


@router.put("/permissions")
def set_permissions(data: dict, request: Request, db: Session = Depends(get_db)):
    _require_superadmin(request)
    # Validate: only known roles and features
    for role, feats in data.items():
        if role not in ("admin", "cashier"):
            raise HTTPException(status_code=400, detail=f"Unknown role: {role}")
        if not isinstance(feats, dict):
            raise HTTPException(status_code=400, detail="Features must be a dict of {feature: bool}")
        for feat, val in feats.items():
            if feat not in DEFAULT_PERMISSIONS.get(role, {}):
                raise HTTPException(status_code=400, detail=f"Unknown feature: {feat}")
            if not isinstance(val, bool):
                raise HTTPException(status_code=400, detail=f"Feature value must be boolean")
    s = db.query(Settings).first()
    if not s:
        raise HTTPException(status_code=500, detail="Settings not found")
    s.permissions = data
    db.commit()
    return _get_permissions(db)


class TOTPVerify(BaseModel):
    code: str


@router.post("/2fa/setup")
def setup_2fa(request: Request, db: Session = Depends(get_db)):
    """Generate a new TOTP secret for the current user and return the provisioning URI."""
    uid = getattr(request.state, "user_id", None)
    if not uid:
        raise HTTPException(status_code=401, detail="Not authenticated")
    u = db.query(User).filter(User.id == uid).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        import pyotp
        secret = pyotp.random_base32()
        totp = pyotp.TOTP(secret)
        gs = db.query(Settings).first()
        issuer = (gs.group_name if gs and gs.group_name else "Equb")
        uri = totp.provisioning_uri(name=u.username, issuer_name=issuer)
        # Store secret (not yet enabled — confirmed by /2fa/verify)
        u.totp_secret = secret
        db.commit()
        return {"secret": secret, "uri": uri}
    except ImportError:
        raise HTTPException(status_code=500, detail="pyotp not installed on server")


@router.post("/2fa/verify")
def verify_2fa(data: TOTPVerify, request: Request, db: Session = Depends(get_db)):
    """Confirm a TOTP code to enable 2FA for the current user."""
    uid = getattr(request.state, "user_id", None)
    if not uid:
        raise HTTPException(status_code=401, detail="Not authenticated")
    _check_2fa_rate(uid)
    u = db.query(User).filter(User.id == uid).first()
    if not u or not u.totp_secret:
        raise HTTPException(status_code=400, detail="Run /2fa/setup first")
    try:
        import pyotp
        totp = pyotp.TOTP(u.totp_secret)
        if not totp.verify(data.code, valid_window=1):
            _record_2fa_failure(uid)
            raise HTTPException(status_code=400, detail="Invalid TOTP code")
        u.totp_enabled = True
        db.commit()
        return {"ok": True, "message": "2FA enabled"}
    except ImportError:
        raise HTTPException(status_code=500, detail="pyotp not installed on server")


@router.post("/2fa/disable")
def disable_2fa(data: TOTPVerify, request: Request, db: Session = Depends(get_db)):
    """Disable 2FA — requires a valid TOTP code to confirm."""
    uid = getattr(request.state, "user_id", None)
    if not uid:
        raise HTTPException(status_code=401, detail="Not authenticated")
    _check_2fa_rate(uid)
    u = db.query(User).filter(User.id == uid).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    if not u.totp_enabled or not u.totp_secret:
        raise HTTPException(status_code=400, detail="2FA is not enabled")
    try:
        import pyotp
        totp = pyotp.TOTP(u.totp_secret)
        if not totp.verify(data.code, valid_window=1):
            _record_2fa_failure(uid)
            raise HTTPException(status_code=400, detail="Invalid TOTP code")
        u.totp_enabled = False
        u.totp_secret = None
        db.commit()
        return {"ok": True, "message": "2FA disabled"}
    except ImportError:
        raise HTTPException(status_code=500, detail="pyotp not installed on server")


@router.get("/2fa/status")
def twofa_status(request: Request, db: Session = Depends(get_db)):
    uid = getattr(request.state, "user_id", None)
    if not uid:
        raise HTTPException(status_code=401, detail="Not authenticated")
    u = db.query(User).filter(User.id == uid).first()
    return {"totp_enabled": bool(u and u.totp_enabled)}


@router.post("/reset-system")
def reset_system(request: Request, db: Session = Depends(get_db)):
    """
    Wipe all equb data and return the system to a clean state.
    User accounts and app settings are preserved.
    Owner (superadmin) access only.
    """
    _require_superadmin(request)

    # Backup before wiping — best-effort, never block the reset
    try:
        from routers.backup import _do_pg_dump, send_backup_email
        import os as _os
        _path = _do_pg_dump("pre_reset")
        if _path:
            send_backup_email(_path, _os.path.basename(_path), db)
    except Exception as _e:
        import logging as _logging
        _logging.getLogger("equb.auth").warning("pre-reset backup failed: %s", _e)

    # Delete in FK-safe order (children before parents)
    db.query(DebtContact).delete(synchronize_session=False)
    db.query(DebtCase).delete(synchronize_session=False)
    db.query(SpotListing).delete(synchronize_session=False)
    db.query(VoucherReturn).delete(synchronize_session=False)
    db.query(PotDisbursement).delete(synchronize_session=False)
    db.query(PotTransaction).delete(synchronize_session=False)
    db.query(Payment).delete(synchronize_session=False)
    db.query(PaymentBatch).delete(synchronize_session=False)
    db.query(DistributionCheque).delete(synchronize_session=False)
    db.query(AssociationExpense).delete(synchronize_session=False)
    db.query(ScheduledNotification).delete(synchronize_session=False)
    db.query(SmsQueue).delete(synchronize_session=False)
    db.query(NotificationLog).delete(synchronize_session=False)
    db.query(AuditLog).delete(synchronize_session=False)
    db.query(MemberSpot).delete(synchronize_session=False)
    db.query(Week).delete(synchronize_session=False)
    db.query(Member).delete(synchronize_session=False)
    db.query(Cycle).delete(synchronize_session=False)
    # Reset all spots to active status (ready for next cycle)
    db.query(Spot).update({"status": "active"}, synchronize_session=False)

    db.commit()
    return {
        "ok": True,
        "message": "System reset complete. All cycles, members, and transactions have been cleared.",
    }

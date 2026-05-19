from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

_ADMIN_ROLES = {"admin", "superadmin"}

# Default permissions when none are stored in the DB.
# superadmin always has full access regardless of this dict.
DEFAULT_PERMISSIONS: dict = {
    "admin": {
        "manage_members":  True,
        "run_draws":       True,
        "disbursements":   True,
        "view_reports":    True,
        "manage_users":    True,
        "notifications":   True,
    },
    "cashier": {
        "manage_members":  False,
        "run_draws":       False,
        "disbursements":   False,
        "view_reports":    True,
        "manage_users":    False,
        "notifications":   False,
    },
}

PERMISSION_LABELS: dict = {
    "manage_members": "Manage Members",
    "run_draws":      "Run Draws",
    "disbursements":  "Disbursements",
    "view_reports":   "View Reports",
    "manage_users":   "Manage Users",
    "notifications":  "Notifications",
}


def _get_permissions(db: Session) -> dict:
    from database import Settings
    s = db.query(Settings).first()
    stored = s.permissions if s else None
    if not stored:
        return DEFAULT_PERMISSIONS
    # Merge stored with defaults so any new features default correctly
    merged = {}
    for role, defaults in DEFAULT_PERMISSIONS.items():
        merged[role] = {**defaults, **(stored.get(role) or {})}
    return merged


def _require_admin(request: Request) -> None:
    if getattr(request.state, "user_role", None) not in _ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin access required")


def _require_superadmin(request: Request) -> None:
    if getattr(request.state, "user_role", None) != "superadmin":
        raise HTTPException(status_code=403, detail="Owner access required")


def _require_feature(request: Request, db: Session, feature: str) -> None:
    role = getattr(request.state, "user_role", None)
    if not role:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if role == "superadmin":
        return
    perms = _get_permissions(db)
    role_perms = perms.get(role, {})
    if not role_perms.get(feature, False):
        raise HTTPException(status_code=403, detail="Access denied")

from fastapi import HTTPException, Request

_ADMIN_ROLES = {"admin", "superadmin"}


def _require_admin(request: Request) -> None:
    if getattr(request.state, "user_role", None) not in _ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin access required")


def _require_superadmin(request: Request) -> None:
    if getattr(request.state, "user_role", None) != "superadmin":
        raise HTTPException(status_code=403, detail="Owner access required")

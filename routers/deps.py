from fastapi import HTTPException, Request


def _require_admin(request: Request) -> None:
    if getattr(request.state, "user_role", None) != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

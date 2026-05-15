from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from database import get_db, User, _pwd  # _pwd is the stdlib-based hasher
from routers.deps import _require_admin

router = APIRouter()


class UserCreate(BaseModel):
    username: str
    password: str
    full_name: str
    role: str = "cashier"


class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None


class PasswordChange(BaseModel):
    current_password: str
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
    _require_admin(request)
    return [_user_dict(u) for u in db.query(User).order_by(User.id).all()]


@router.post("/users")
def create_user(data: UserCreate, request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    if db.query(User).filter(User.username == data.username).first():
        raise HTTPException(status_code=400, detail="Username already exists")
    _validate_password(data.password)
    u = User(
        username=data.username,
        password_hash=_pwd.hash(data.password),
        full_name=data.full_name,
        role=data.role,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return _user_dict(u)


@router.put("/users/{user_id}")
def update_user(user_id: int, data: UserUpdate, request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    for field, val in data.model_dump(exclude_none=True).items():
        setattr(u, field, val)
    db.commit()
    db.refresh(u)
    return _user_dict(u)


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

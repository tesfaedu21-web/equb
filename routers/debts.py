from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import (
    get_db, Member, Payment, Week, Cycle, MemberSpot,
    DebtCase, DebtContact, User,
)
from routers.deps import _get_current_user

router = APIRouter()


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── Pydantic models ───────────────────────────────────────────────────────────

class CaseUpdate(BaseModel):
    status: Optional[str] = None
    promise_date: Optional[str] = None
    notes: Optional[str] = None


class ContactCreate(BaseModel):
    method: str                          # phone | in_person | sms | email | letter
    outcome: str                         # contacted | no_answer | promise | refused | partial_payment
    promised_amount: Optional[float] = None
    promise_date: Optional[str] = None
    notes: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _actual_owed(member_id: int, cycle_id: Optional[int], db: Session) -> float:
    q = (db.query(func.sum(Payment.amount))
         .join(Week)
         .filter(Payment.member_id == member_id,
                 Payment.status.in_(["missed", "late"])))
    if cycle_id:
        q = q.filter(Week.cycle_id == cycle_id)
    return float(q.scalar() or 0)


def _case_dict(c: DebtCase, db: Session) -> dict:
    owed = _actual_owed(c.member_id, c.cycle_id, db)
    last = c.contacts[-1] if c.contacts else None
    return {
        "id":           c.id,
        "member_id":    c.member_id,
        "member_name":  c.member.name if c.member else None,
        "phone":        c.member.phone if c.member else None,
        "cycle_id":     c.cycle_id,
        "status":       c.status,
        "total_owed":   owed,
        "promise_date": c.promise_date.isoformat() if c.promise_date else None,
        "resolved_at":  c.resolved_at.isoformat() if c.resolved_at else None,
        "notes":        c.notes,
        "contact_count": len(c.contacts),
        "last_contact": last.contact_date.isoformat() if last else None,
        "created_at":   c.created_at.isoformat() if c.created_at else None,
    }


def _contact_dict(ct: DebtContact) -> dict:
    return {
        "id":             ct.id,
        "contact_date":   ct.contact_date.isoformat() if ct.contact_date else None,
        "method":         ct.method,
        "outcome":        ct.outcome,
        "promised_amount": ct.promised_amount,
        "promise_date":   ct.promise_date.isoformat() if ct.promise_date else None,
        "notes":          ct.notes,
        "logged_by":      ct.logged_by.full_name if ct.logged_by else None,
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("")
def list_cases(status: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(DebtCase)
    if status:
        q = q.filter(DebtCase.status == status)
    cases = q.order_by(DebtCase.updated_at.desc()).all()
    return [_case_dict(c, db) for c in cases]


@router.post("/generate")
def generate_cases(request: Request, db: Session = Depends(get_db)):
    """Auto-create debt cases for every member with missed/late payments in the active cycle."""
    active = db.query(Cycle).filter(Cycle.status == "active").first()
    if not active:
        raise HTTPException(400, "No active cycle")

    rows = (
        db.query(Payment.member_id, func.sum(Payment.amount).label("owed"))
        .join(Week)
        .filter(Week.cycle_id == active.id,
                Payment.status.in_(["missed", "late"]))
        .group_by(Payment.member_id)
        .all()
    )

    created = 0
    updated = 0
    for member_id, owed in rows:
        existing = (
            db.query(DebtCase)
            .filter_by(member_id=member_id, cycle_id=active.id)
            .filter(DebtCase.status.notin_(["resolved", "written_off"]))
            .first()
        )
        if existing:
            existing.total_owed = float(owed)
            updated += 1
        else:
            db.add(DebtCase(
                member_id=member_id,
                cycle_id=active.id,
                total_owed=float(owed),
            ))
            created += 1

    db.commit()
    return {"created": created, "updated": updated}


@router.get("/summary")
def summary(db: Session = Depends(get_db)):
    cases = db.query(DebtCase).all()
    total_owed  = sum(_actual_owed(c.member_id, c.cycle_id, db) for c in cases
                      if c.status not in ("resolved", "written_off"))
    return {
        "open":            sum(1 for c in cases if c.status == "open"),
        "promise_to_pay":  sum(1 for c in cases if c.status == "promise_to_pay"),
        "escalated":       sum(1 for c in cases if c.status == "escalated"),
        "resolved":        sum(1 for c in cases if c.status == "resolved"),
        "written_off":     sum(1 for c in cases if c.status == "written_off"),
        "total_owed":      total_owed,
    }


@router.get("/{case_id}")
def get_case(case_id: int, db: Session = Depends(get_db)):
    c = db.query(DebtCase).filter(DebtCase.id == case_id).first()
    if not c:
        raise HTTPException(404, "Case not found")
    result = _case_dict(c, db)
    result["contacts"] = [_contact_dict(ct) for ct in reversed(c.contacts)]
    return result


@router.put("/{case_id}")
def update_case(case_id: int, data: CaseUpdate, db: Session = Depends(get_db)):
    c = db.query(DebtCase).filter(DebtCase.id == case_id).first()
    if not c:
        raise HTTPException(404, "Case not found")
    if data.status is not None:
        c.status = data.status
        if data.status in ("resolved", "written_off") and not c.resolved_at:
            c.resolved_at = _utcnow()
        elif data.status not in ("resolved", "written_off"):
            c.resolved_at = None
    if data.promise_date is not None:
        c.promise_date = datetime.fromisoformat(data.promise_date) if data.promise_date else None
    if data.notes is not None:
        c.notes = data.notes
    db.commit()
    return _case_dict(c, db)


@router.post("/{case_id}/contacts")
def log_contact(case_id: int, data: ContactCreate, request: Request,
                db: Session = Depends(get_db)):
    c = db.query(DebtCase).filter(DebtCase.id == case_id).first()
    if not c:
        raise HTTPException(404, "Case not found")

    user = _get_current_user(request, db)

    promise_dt = None
    if data.promise_date:
        promise_dt = datetime.fromisoformat(data.promise_date)

    ct = DebtContact(
        case_id=case_id,
        contact_date=_utcnow(),
        method=data.method,
        outcome=data.outcome,
        promised_amount=data.promised_amount,
        promise_date=promise_dt,
        notes=data.notes,
        logged_by_id=user.id if user else None,
    )
    db.add(ct)

    # Auto-advance case status on certain outcomes
    if data.outcome == "promise" and c.status == "open":
        c.status = "promise_to_pay"
        if promise_dt and not c.promise_date:
            c.promise_date = promise_dt

    db.commit()
    return _contact_dict(ct)


@router.delete("/{case_id}/contacts/{contact_id}")
def delete_contact(case_id: int, contact_id: int, db: Session = Depends(get_db)):
    ct = (db.query(DebtContact)
          .filter(DebtContact.id == contact_id, DebtContact.case_id == case_id)
          .first())
    if not ct:
        raise HTTPException(404, "Contact not found")
    db.delete(ct)
    db.commit()
    return {"ok": True}

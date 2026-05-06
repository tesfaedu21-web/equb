from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import or_
from pydantic import BaseModel
from typing import Optional, List
from database import get_db, Member, MemberSpot, Spot, Settings, Payment, Cycle
import csv, io, openpyxl
from openpyxl.styles import Font, PatternFill, Alignment

router = APIRouter()


class SpotAssignment(BaseModel):
    spot_id: int
    share: str = "full"           # full | half
    weekly_contribution: float = 21000


class MemberCreate(BaseModel):
    name: str
    phone: Optional[str] = None
    spots: List[SpotAssignment] = []
    notes: Optional[str] = None


class MemberUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None


class SpotAdd(BaseModel):
    spot_id: int
    share: str = "full"
    weekly_contribution: float = 21000


def _member_dict(m: Member, cycle_id: Optional[int] = None) -> dict:
    # Filter assignments by cycle if provided; otherwise fall back to any active (legacy)
    if cycle_id is not None:
        active_sa = [sa for sa in m.spot_assignments if sa.is_active and sa.cycle_id == cycle_id]
    else:
        active_sa = [sa for sa in m.spot_assignments if sa.is_active]

    assignments = [
        {
            "id": sa.id,
            "spot_id": sa.spot_id,
            "spot_number": sa.spot.number if sa.spot else None,
            "spot_type": sa.spot.spot_type if sa.spot else None,
            "share": sa.share,
            "weekly_contribution": sa.weekly_contribution,
            "cycle_id": sa.cycle_id,
        }
        for sa in active_sa
    ]
    total_weekly = sum(a["weekly_contribution"] for a in assignments)
    spot_numbers = [a["spot_number"] for a in assignments]

    # Find half-spot partners within the same cycle
    partners = []
    for sa in active_sa:
        if sa.share == "half" and sa.spot:
            for other_sa in sa.spot.spot_assignments:
                if (other_sa.member_id != m.id and other_sa.is_active
                        and (cycle_id is None or other_sa.cycle_id == cycle_id)):
                    partners.append(other_sa.member.name)

    return {
        "id": m.id,
        "name": m.name,
        "phone": m.phone,
        "status": m.status,
        "spots": assignments,
        "spot_numbers": spot_numbers,
        "total_weekly_contribution": total_weekly,
        "spot_count": len(assignments),
        "partners": list(set(partners)),
        "notes": m.notes,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


@router.get("")
def list_members(search: str = "", status: str = "",
                 cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    q = db.query(Member)
    if search:
        q = q.filter(or_(Member.name.ilike(f"%{search}%"), Member.phone.ilike(f"%{search}%")))
    if status:
        q = q.filter(Member.status == status)
    if cycle_id:
        # Only members who have at least one active assignment in this cycle
        member_ids_in_cycle = [
            r[0] for r in db.query(MemberSpot.member_id).filter(
                MemberSpot.cycle_id == cycle_id,
                MemberSpot.is_active == True,
            ).distinct().all()
        ]
        q = q.filter(Member.id.in_(member_ids_in_cycle))
    members = q.order_by(Member.name).all()
    return [_member_dict(m, cycle_id) for m in members]


@router.get("/stats")
def member_stats(cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    if cycle_id:
        # Count only members who have an active assignment in this cycle
        member_ids = [
            r[0] for r in db.query(MemberSpot.member_id).filter(
                MemberSpot.cycle_id == cycle_id,
                MemberSpot.is_active == True,
            ).distinct().all()
        ]
        members = db.query(Member).filter(Member.id.in_(member_ids)).all()
        total    = len(members)
        active   = sum(1 for m in members if m.status == "active")
        received = sum(1 for m in members if m.status == "received")
        left     = sum(1 for m in members if m.status == "left")
        total_spots_assigned = db.query(MemberSpot).filter(
            MemberSpot.cycle_id == cycle_id, MemberSpot.is_active == True
        ).count()
    else:
        total    = db.query(Member).filter(Member.status != "left").count()
        active   = db.query(Member).filter(Member.status == "active").count()
        received = db.query(Member).filter(Member.status == "received").count()
        left     = db.query(Member).filter(Member.status == "left").count()
        total_spots_assigned = db.query(MemberSpot).filter(MemberSpot.is_active == True).count()
    return {
        "total": total, "active": active, "received": received, "left": left,
        "total_spots_assigned": total_spots_assigned,
    }


@router.get("/available-spots")
def available_spots(db: Session = Depends(get_db)):
    # Scope availability to the active cycle's memberships
    cycle = db.query(Cycle).filter(Cycle.status == "active").first()
    spots = db.query(Spot).filter(Spot.status == "active").order_by(Spot.number).all()
    result = []
    for s in spots:
        # Only consider assignments belonging to the active cycle (or legacy NULL)
        if cycle:
            active_assignments = [sa for sa in s.spot_assignments
                                  if sa.is_active and sa.cycle_id == cycle.id]
        else:
            active_assignments = [sa for sa in s.spot_assignments if sa.is_active]

        # A full-share occupant locks the spot exclusively — not available to anyone else
        has_full_occupant = any(sa.share == "full" for sa in active_assignments)
        if has_full_occupant:
            continue

        # Only half-share occupants remain
        half_count = len(active_assignments)
        if half_count < 2:
            result.append({
                "id": s.id,
                "number": s.number,
                "type": s.spot_type,
                "occupants": half_count,
                "is_half_available": half_count == 1,
                "current_members": [sa.member.name for sa in active_assignments],
            })
    return result


# ── Export (must be before /{member_id} routes) ──────────────────────────────

@router.get("/export")
def export_members(format: str = Query("csv", pattern="^(csv|xlsx)$"), db: Session = Depends(get_db)):
    """Download member list as CSV or Excel."""
    members = db.query(Member).order_by(Member.id).all()

    headers_row = ["Name", "Phone", "Status", "Spot Numbers", "Share Types",
                   "Weekly Contribution (ETB)", "Partners", "Notes"]

    def _row(m):
        assignments = [sa for sa in m.spot_assignments if sa.is_active]
        return [
            m.name,
            m.phone or "",
            m.status,
            ", ".join(str(sa.spot.number) for sa in assignments),
            ", ".join(sa.share for sa in assignments),
            sum(sa.weekly_contribution for sa in assignments),
            ", ".join(sorted(set(
                other.member.name
                for sa in assignments if sa.share == "half"
                for other in sa.spot.spot_assignments
                if other.is_active and other.member_id != m.id
            ))),
            m.notes or "",
        ]

    if format == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(headers_row)
        for m in members:
            w.writerow(_row(m))
        content = buf.getvalue().encode("utf-8-sig")
        return StreamingResponse(
            io.BytesIO(content), media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=members_export.csv"},
        )

    # Excel
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Members"
    header_fill = PatternFill("solid", fgColor="1a7a4a")
    header_font = Font(bold=True, color="FFFFFF")
    for col, h in enumerate(headers_row, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")
    for r, m in enumerate(members, 2):
        for col, val in enumerate(_row(m), 1):
            ws.cell(row=r, column=col, value=val)
    col_widths = [30, 18, 12, 16, 14, 26, 30, 24]
    for i, w_val in enumerate(col_widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w_val
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=members_export.xlsx"},
    )


@router.get("/import-template")
def import_template(db: Session = Depends(get_db)):
    """Download a blank CSV import template with example row."""
    spots = db.query(Spot).filter(Spot.status == "active").order_by(Spot.number).all()
    taken = {sa.spot.number for s in spots for sa in s.spot_assignments if sa.is_active}
    example_spot = next((s.number for s in spots if s.number not in taken), "")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Name", "Phone", "Spot Number", "Share", "Notes"])
    w.writerow(["Example Member", "+251912345678", example_spot, "full",
                "Example row — delete before importing"])
    content = buf.getvalue().encode("utf-8-sig")
    return StreamingResponse(
        io.BytesIO(content), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=import_template.csv"},
    )


# ── Import (must be before /{member_id} routes) ───────────────────────────────

@router.post("/import")
async def import_members(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Import members from CSV or Excel. Returns per-row results."""
    settings = db.query(Settings).first()
    filename = (file.filename or "").lower()
    content = await file.read()

    rows = []
    try:
        if filename.endswith(".csv"):
            text = content.decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text))
            rows = [r for r in reader if any(v and str(v).strip() for v in r.values())]
        elif filename.endswith((".xlsx", ".xls")):
            wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            ws = wb.active
            iter_rows = list(ws.iter_rows(values_only=True))
            if not iter_rows:
                raise HTTPException(400, "Empty file")
            headers = [str(h).strip() if h else "" for h in iter_rows[0]]
            for row in iter_rows[1:]:
                if any(cell is not None for cell in row):
                    rows.append(dict(zip(headers,
                        [str(c).strip() if c is not None else "" for c in row])))
        else:
            raise HTTPException(400, "Only .csv or .xlsx files are supported")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Could not parse file: {e}")

    def _get(row, *keys):
        for k in keys:
            for rk in row:
                if rk and rk.strip().lower() == k.lower():
                    v = str(row[rk]).strip() if row[rk] else ""
                    if v:
                        return v
        return ""

    # Get active cycle for spot assignments
    active_cycle = db.query(Cycle).filter(Cycle.status == "active").first()
    active_cycle_id = active_cycle.id if active_cycle else None

    results = []
    created = 0
    skipped = 0

    for i, row in enumerate(rows, 1):
        name     = _get(row, "Name", "Full Name", "member name", "member")
        phone    = _get(row, "Phone", "Phone Number", "mobile", "tel")
        spot_str = _get(row, "Spot Number", "Spot", "spot_number", "spot no")
        share    = (_get(row, "Share", "Share Type", "share_type") or "full").lower().strip()
        notes    = _get(row, "Notes", "note", "remark")

        if share not in ("full", "half"):
            share = "full"

        if not name:
            results.append({"row": i, "status": "skipped", "reason": "Empty name"})
            skipped += 1
            continue

        m = Member(name=name, phone=phone or None, notes=notes or None)
        db.add(m)
        db.flush()

        spot_msg = None
        if spot_str:
            try:
                spot_num = int(float(spot_str))
                spot = db.query(Spot).filter(Spot.number == spot_num).first()
                if not spot:
                    spot_msg = f"Spot #{spot_num} not found — created without spot"
                else:
                    active_count = sum(1 for sa in spot.spot_assignments if sa.is_active)
                    if active_count >= 2:
                        spot_msg = f"Spot #{spot_num} is full — created without spot"
                    else:
                        if active_count == 1 and share == "full":
                            share = "half"
                            spot_msg = f"Spot #{spot_num} has 1 occupant — assigned as half"
                        else:
                            spot_msg = f"Spot #{spot_num} ({share})"
                        contribution = (
                            (settings.full_spot_amount if settings else 21000)
                            if share == "full"
                            else (settings.half_spot_amount if settings else 10500)
                        )
                        db.add(MemberSpot(member_id=m.id, spot_id=spot.id,
                                          share=share, weekly_contribution=contribution,
                                          cycle_id=active_cycle_id))
            except (ValueError, TypeError):
                spot_msg = f"Invalid spot number '{spot_str}' — created without spot"

        results.append({
            "row": i, "status": "created",
            "name": name, "phone": phone or None,
            "spot": spot_msg,
            "warning": ("⚠ " + spot_msg) if spot_msg and "without" in spot_msg else None,
        })
        created += 1

    db.commit()
    return {"total": len(rows), "created": created, "skipped": skipped, "results": results}


# ── CRUD (parameterised routes — must be AFTER all fixed-path routes) ─────────

@router.delete("/permanent/all")
def delete_all_members(request: Request, db: Session = Depends(get_db)):
    """Wipe all members. Admin only.
    Members with no payment records are hard-deleted.
    Members that have payment records are soft-deleted (marked as left).
    """
    if getattr(request.state, "user_role", None) != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    # Remove all spot assignments first
    db.query(MemberSpot).delete(synchronize_session=False)
    db.flush()

    all_members = db.query(Member).all()
    hard_deleted = 0
    soft_deleted = 0
    for m in all_members:
        pay_count = db.query(Payment).filter(Payment.member_id == m.id).count()
        if pay_count == 0:
            db.delete(m)
            hard_deleted += 1
        else:
            m.status = "left"
            soft_deleted += 1

    db.commit()
    return {"ok": True, "hard_deleted": hard_deleted, "soft_deleted": soft_deleted,
            "message": f"{hard_deleted} permanently deleted, {soft_deleted} marked as left (had payment records)"}


@router.delete("/permanent/{member_id}")
def delete_member_permanent(member_id: int, request: Request, db: Session = Depends(get_db)):
    """Hard-delete a member. Admin only. Blocked if any payment records exist."""
    if getattr(request.state, "user_role", None) != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    m = db.query(Member).filter(Member.id == member_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="Member not found")
    pay_count = db.query(Payment).filter(Payment.member_id == member_id).count()
    if pay_count > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete: member has {pay_count} payment record(s). "
                   "Use 'Mark as Left' to deactivate instead.",
        )
    db.query(MemberSpot).filter(MemberSpot.member_id == member_id).delete(synchronize_session=False)
    db.delete(m)
    db.commit()
    return {"ok": True}


@router.post("")
def create_member(data: MemberCreate, db: Session = Depends(get_db)):
    settings = db.query(Settings).first()
    cycle = db.query(Cycle).filter(Cycle.status == "active").first()
    m = Member(name=data.name, phone=data.phone, notes=data.notes)
    db.add(m)
    db.flush()

    for assignment in data.spots:
        spot = db.query(Spot).filter(Spot.id == assignment.spot_id).first()
        if not spot:
            raise HTTPException(status_code=404, detail=f"Spot {assignment.spot_id} not found")
        cycle_id_val = cycle.id if cycle else None
        active_assignments = [sa for sa in spot.spot_assignments
                              if sa.is_active and sa.cycle_id == cycle_id_val]
        has_full = any(sa.share == "full" for sa in active_assignments)
        half_count = sum(1 for sa in active_assignments if sa.share == "half")

        if has_full:
            raise HTTPException(status_code=400,
                detail=f"Spot #{spot.number} is fully occupied by a full-spot member — it cannot be shared.")
        if assignment.share == "full" and half_count > 0:
            raise HTTPException(status_code=400,
                detail=f"Spot #{spot.number} already has a half-spot member — assign a different spot for a full spot.")
        if assignment.share == "half" and half_count >= 2:
            raise HTTPException(status_code=400,
                detail=f"Spot #{spot.number} is full (2 half-spot members already).")
        # Enforce contribution amount from settings (not client-sent value)
        contribution = (
            (settings.full_spot_amount if settings else 21000)
            if assignment.share == "full"
            else (settings.half_spot_amount if settings else 10500)
        )
        db.add(MemberSpot(
            member_id=m.id, spot_id=assignment.spot_id,
            share=assignment.share, weekly_contribution=contribution,
            cycle_id=cycle.id if cycle else None,   # ← scoped to active cycle
        ))

    db.commit()
    db.refresh(m)
    return _member_dict(m)


@router.get("/{member_id}")
def get_member(member_id: int, db: Session = Depends(get_db)):
    m = db.query(Member).filter(Member.id == member_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="Member not found")
    return _member_dict(m)


@router.put("/{member_id}")
def update_member(member_id: int, data: MemberUpdate, db: Session = Depends(get_db)):
    m = db.query(Member).filter(Member.id == member_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="Member not found")
    for field, value in data.model_dump(exclude_none=True).items():
        setattr(m, field, value)
    db.commit()
    db.refresh(m)
    return _member_dict(m)


@router.post("/{member_id}/spots")
def add_spot(member_id: int, data: SpotAdd, db: Session = Depends(get_db)):
    m = db.query(Member).filter(Member.id == member_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="Member not found")
    spot = db.query(Spot).filter(Spot.id == data.spot_id).first()
    if not spot:
        raise HTTPException(status_code=404, detail="Spot not found")

    settings = db.query(Settings).first()
    cycle = db.query(Cycle).filter(Cycle.status == "active").first()
    # Enforce contribution amount from settings
    contribution = (
        (settings.full_spot_amount if settings else 21000)
        if data.share == "full"
        else (settings.half_spot_amount if settings else 10500)
    )

    existing = db.query(MemberSpot).filter_by(member_id=member_id, spot_id=data.spot_id,
                                               cycle_id=cycle.id if cycle else None).first()
    if existing:
        if existing.is_active:
            raise HTTPException(status_code=400, detail="Member already assigned to this spot")
        existing.is_active = True
        existing.share = data.share
        existing.weekly_contribution = contribution
    else:
        cycle_id_val = cycle.id if cycle else None
        active_assignments = [sa for sa in spot.spot_assignments
                              if sa.is_active and sa.member_id != member_id
                              and sa.cycle_id == cycle_id_val]
        has_full = any(sa.share == "full" for sa in active_assignments)
        half_count = sum(1 for sa in active_assignments if sa.share == "half")

        if has_full:
            raise HTTPException(status_code=400,
                detail=f"Spot #{spot.number} is fully occupied by a full-spot member — it cannot be shared.")
        if data.share == "full" and half_count > 0:
            raise HTTPException(status_code=400,
                detail=f"Spot #{spot.number} already has a half-spot member — choose a different spot for a full spot.")
        if data.share == "half" and half_count >= 2:
            raise HTTPException(status_code=400,
                detail=f"Spot #{spot.number} is full (2 half-spot members already).")
        db.add(MemberSpot(
            member_id=member_id, spot_id=data.spot_id,
            share=data.share, weekly_contribution=contribution,
            cycle_id=cycle.id if cycle else None,   # ← scoped to active cycle
        ))

    db.commit()
    db.refresh(m)
    return _member_dict(m)


@router.delete("/{member_id}/spots/{spot_id}")
def remove_spot(member_id: int, spot_id: int, db: Session = Depends(get_db)):
    sa = db.query(MemberSpot).filter_by(member_id=member_id, spot_id=spot_id).first()
    if not sa:
        raise HTTPException(status_code=404, detail="Spot assignment not found")
    sa.is_active = False
    db.commit()
    return {"ok": True}


@router.delete("/{member_id}")
def mark_left(member_id: int, db: Session = Depends(get_db)):
    m = db.query(Member).filter(Member.id == member_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="Member not found")
    m.status = "left"
    for sa in m.spot_assignments:
        sa.is_active = False
    db.commit()
    return {"ok": True}

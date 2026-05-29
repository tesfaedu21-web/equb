from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import (
    get_db, Cycle, Week, Spot, Member, MemberSpot, Payment,
    PotTransaction, Settings, SpotListing, cycle_cfg,
)
from routers.deps import _require_feature, _get_current_user

router = APIRouter()


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── Pydantic models ───────────────────────────────────────────────────────────

class ListingCreate(BaseModel):
    cycle_id: int
    week_id: Optional[int] = None
    spot_id: Optional[int] = None
    seller_id: Optional[int] = None
    listing_type: str = "member_sale"   # member_sale | group_week_sale | assoc_spot_sale
    asking_price: Optional[float] = None
    percentage: Optional[float] = Field(None, ge=0, le=100)
    notes: Optional[str] = None


class ListingUpdate(BaseModel):
    asking_price: Optional[float] = None
    percentage: Optional[float] = Field(None, ge=0, le=100)
    notes: Optional[str] = None
    status: Optional[str] = None


class CompleteSale(BaseModel):
    buyer_id: int
    percentage: Optional[float] = Field(None, ge=0, le=100)
    spot_id: Optional[int] = None       # buyer's spot (group_week_sale)
    notes: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _listing_dict(l: SpotListing) -> dict:
    week = l.week
    spot = l.spot
    seller = l.seller
    buyer = l.buyer
    spot_members = []
    if spot and week:
        spot_members = [
            {"id": sa.member.id, "name": sa.member.name, "share": sa.share}
            for sa in spot.spot_assignments
            if sa.is_active and sa.cycle_id == l.cycle_id
        ]
    return {
        "id":           l.id,
        "cycle_id":     l.cycle_id,
        "week_id":      l.week_id,
        "week_number":  week.week_number if week else None,
        "draw_date":    week.draw_date.isoformat() if week else None,
        "spot_id":      l.spot_id,
        "spot_number":  spot.number if spot else None,
        "spot_type":    spot.spot_type if spot else None,
        "spot_members": spot_members,
        "seller_id":    l.seller_id,
        "seller_name":  seller.name if seller else None,
        "seller_phone": seller.phone if seller else None,
        "listing_type": l.listing_type,
        "asking_price": l.asking_price,
        "percentage":   l.percentage,
        "status":       l.status,
        "notes":        l.notes,
        "buyer_id":     l.buyer_id,
        "buyer_name":   buyer.name if buyer else None,
        "sold_price":   l.sold_price,
        "listed_at":    l.listed_at.isoformat() if l.listed_at else None,
        "sold_at":      l.sold_at.isoformat() if l.sold_at else None,
        "net_pot":      week.net_pot if week else None,
        "gross_pot":    week.gross_pot if week else None,
    }


def _check_fully_paid(member, up_to_week_number: int, cycle_id, db: Session) -> dict:
    q = (db.query(Payment).join(Week)
         .filter(Payment.member_id == member.id,
                 Payment.status.in_(["pending", "late", "missed"]),
                 Week.week_number <= up_to_week_number))
    if cycle_id:
        q = q.filter(Week.cycle_id == cycle_id)
    unpaid = q.all()
    return {
        "fully_paid": len(unpaid) == 0,
        "unpaid_count": len(unpaid),
        "unpaid_weeks": sorted(p.week.week_number for p in unpaid),
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/listings")
def list_listings(status: Optional[str] = None, cycle_id: Optional[int] = None,
                  db: Session = Depends(get_db)):
    q = db.query(SpotListing)
    if status:
        q = q.filter(SpotListing.status == status)
    if cycle_id:
        q = q.filter(SpotListing.cycle_id == cycle_id)
    listings = q.order_by(SpotListing.listed_at.desc()).all()
    return [_listing_dict(l) for l in listings]


@router.get("/summary")
def summary(cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    if not cycle_id:
        active = db.query(Cycle).filter(Cycle.status == "active").order_by(Cycle.id.desc()).first()
        cycle_id = active.id if active else None

    q = db.query(SpotListing)
    if cycle_id:
        q = q.filter(SpotListing.cycle_id == cycle_id)
    listings = q.all()

    # Drawn weeks in this cycle that don't yet have a pot transaction
    tx_week_ids = {
        r[0] for r in db.query(PotTransaction.week_id).all()
    }
    drawn_weeks = []
    if cycle_id:
        for w in db.query(Week).filter(
            Week.cycle_id == cycle_id,
            Week.status.in_(["drawn", "pending"]),
        ).all():
            if w.id not in tx_week_ids:
                drawn_weeks.append({
                    "id": w.id,
                    "week_number": w.week_number,
                    "draw_date": w.draw_date.isoformat(),
                    "status": w.status,
                    "is_group_week": w.is_group_week,
                    "net_pot": w.net_pot,
                    "has_listing": any(l.week_id == w.id for l in listings),
                })

    return {
        "open":         sum(1 for l in listings if l.status == "open"),
        "sold":         sum(1 for l in listings if l.status == "sold"),
        "cancelled":    sum(1 for l in listings if l.status == "cancelled"),
        "total_sold_value": sum((l.sold_price or 0) for l in listings if l.status == "sold"),
        "available_weeks": drawn_weeks,
    }


@router.post("/listings")
def create_listing(data: ListingCreate, request: Request, db: Session = Depends(get_db)):
    _require_feature(request, db, "run_draws")

    cycle = db.query(Cycle).filter(Cycle.id == data.cycle_id).first()
    if not cycle:
        raise HTTPException(404, "Cycle not found")

    week = None
    if data.week_id:
        week = db.query(Week).filter(Week.id == data.week_id).first()
        if not week or week.cycle_id != data.cycle_id:
            raise HTTPException(400, "Week not found in this cycle")

    spot = None
    if data.spot_id:
        spot = db.query(Spot).filter(Spot.id == data.spot_id).first()
        if not spot:
            raise HTTPException(404, "Spot not found")

    seller = None
    if data.seller_id:
        seller = db.query(Member).filter(Member.id == data.seller_id).first()
        if not seller:
            raise HTTPException(404, "Seller not found")

    listing = SpotListing(
        cycle_id=data.cycle_id,
        week_id=data.week_id,
        spot_id=data.spot_id,
        seller_id=data.seller_id,
        listing_type=data.listing_type,
        asking_price=data.asking_price,
        percentage=data.percentage,
        notes=data.notes,
    )
    db.add(listing)
    db.commit()
    db.refresh(listing)
    return _listing_dict(listing)


@router.put("/listings/{listing_id}")
def update_listing(listing_id: int, data: ListingUpdate, request: Request,
                   db: Session = Depends(get_db)):
    _require_feature(request, db, "run_draws")
    l = db.query(SpotListing).filter(SpotListing.id == listing_id).first()
    if not l:
        raise HTTPException(404, "Listing not found")
    if data.asking_price is not None:
        l.asking_price = data.asking_price
    if data.percentage is not None:
        l.percentage = data.percentage
    if data.notes is not None:
        l.notes = data.notes
    if data.status is not None:
        l.status = data.status
        if data.status == "sold" and not l.sold_at:
            l.sold_at = _utcnow()
    db.commit()
    return _listing_dict(l)


@router.post("/listings/{listing_id}/complete")
def complete_sale(listing_id: int, data: CompleteSale, request: Request,
                  db: Session = Depends(get_db)):
    """Record the actual pot sale transaction and mark listing as sold."""
    _require_feature(request, db, "run_draws")

    l = db.query(SpotListing).filter(SpotListing.id == listing_id).first()
    if not l:
        raise HTTPException(404, "Listing not found")
    if l.status != "open":
        raise HTTPException(400, f"Listing is already {l.status}")
    if not l.week_id:
        raise HTTPException(400, "Listing has no associated week — cannot record a transaction")

    w = db.query(Week).filter(Week.id == l.week_id).first()
    if not w:
        raise HTTPException(404, "Week not found")
    if w.status not in ("pending", "drawn"):
        raise HTTPException(400, "Week has already been fully processed")

    buyer = db.query(Member).filter(Member.id == data.buyer_id).first()
    if not buyer:
        raise HTTPException(404, "Buyer not found")
    if buyer.status == "left":
        raise HTTPException(400, f"{buyer.name} has left the group")
    if buyer.status != "active":
        raise HTTPException(400, f"{buyer.name} is not an active member (already received)")

    # Buyer must be in this cycle
    buyer_sas = db.query(MemberSpot).filter(
        MemberSpot.member_id == buyer.id,
        MemberSpot.cycle_id == w.cycle_id,
        MemberSpot.is_active == True,
    ).all()
    if not buyer_sas:
        raise HTTPException(400, f"{buyer.name} is not a member of this cycle")

    # Buyer must be fully paid
    pay_check = _check_fully_paid(buyer, w.week_number, w.cycle_id, db)
    if not pay_check["fully_paid"]:
        raise HTTPException(
            400, f"{buyer.name} has {pay_check['unpaid_count']} unpaid week(s): "
                 f"{pay_check['unpaid_weeks']}"
        )

    # Calculate amounts
    gs = db.query(Settings).first()
    cfg = cycle_cfg(w.cycle, gs)
    total_weeks_count = db.query(Week).filter(Week.cycle_id == w.cycle_id).count()

    service_fee_buyer = sum(
        cfg.full_spot_amount if sa.share == "full" else cfg.half_spot_amount
        for sa in buyer_sas
    )
    voucher_buyer = sum(
        (cfg.full_spot_voucher if sa.share == "full" else cfg.half_spot_voucher) * total_weeks_count
        for sa in buyer_sas
    )

    gross = float(w.gross_pot or 0)
    pct = data.percentage if data.percentage is not None else float(l.percentage or 0)
    seller_fee = gross * (pct / 100) if pct else 0.0
    buyer_receives = float(w.net_pot or 0) - float(service_fee_buyer) - float(voucher_buyer) - seller_fee

    tx_type = l.listing_type or "member_sale"

    tx = PotTransaction(
        week_id=w.id,
        transaction_type=tx_type,
        original_winner_id=l.seller_id,
        seller_id=l.seller_id,
        buyer_id=buyer.id,
        percentage=pct or None,
        gross_amount=gross,
        seller_fee=seller_fee,
        buyer_receives=buyer_receives,
        notes=data.notes or l.notes,
    )
    db.add(tx)

    # Mark buyer's payment for this week as paid
    buyer_payment = db.query(Payment).filter(
        Payment.member_id == buyer.id,
        Payment.week_id == w.id,
    ).first()
    if buyer_payment and buyer_payment.status != "paid":
        buyer_payment.status = "paid"
        buyer_payment.payment_method = "pot_sale"
        buyer_payment.paid_date = _utcnow()

    # Update spot / member statuses
    if tx_type == "group_week_sale" and data.spot_id:
        covered = {sa.spot_id for sa in buyer_sas}
        if data.spot_id not in covered:
            raise HTTPException(400, "spot_id does not belong to this buyer")
        for sa in buyer_sas:
            if sa.spot_id == data.spot_id:
                sa.spot.status = "received"
        if not w.winner_spot_id:
            w.winner_spot_id = data.spot_id
    elif tx_type == "assoc_spot_sale":
        if w.winner_spot_id:
            assoc_spot = db.query(Spot).filter(
                Spot.id == w.winner_spot_id, Spot.spot_type == "association"
            ).first()
            if assoc_spot:
                assoc_spot.status = "received"
    else:
        # member_sale: buyer takes the seller's spot
        buyer.status = "received"
        for sa in buyer_sas:
            sa.spot.status = "received"

    w.status = "sold"

    # Mark listing as sold
    l.status = "sold"
    l.buyer_id = buyer.id
    l.sold_price = buyer_receives
    l.percentage = pct or None
    l.sold_at = _utcnow()

    db.commit()

    return {
        "ok": True,
        "listing_id": l.id,
        "week_number": w.week_number,
        "buyer": buyer.name,
        "buyer_receives": buyer_receives,
        "seller_fee": seller_fee,
    }


@router.delete("/listings/{listing_id}")
def cancel_listing(listing_id: int, request: Request, db: Session = Depends(get_db)):
    _require_feature(request, db, "run_draws")
    l = db.query(SpotListing).filter(SpotListing.id == listing_id).first()
    if not l:
        raise HTTPException(404, "Listing not found")
    if l.status == "sold":
        raise HTTPException(400, "Cannot cancel a completed sale")
    l.status = "cancelled"
    db.commit()
    return {"ok": True}


@router.get("/active-members")
def buyers_list(cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Active members eligible to buy (status=active, in-cycle)."""
    if not cycle_id:
        active = db.query(Cycle).filter(Cycle.status == "active").order_by(Cycle.id.desc()).first()
        cycle_id = active.id if active else None
    if cycle_id:
        member_ids = [r[0] for r in db.query(MemberSpot.member_id).filter(
            MemberSpot.cycle_id == cycle_id, MemberSpot.is_active == True
        ).distinct().all()]
        members = db.query(Member).filter(
            Member.id.in_(member_ids), Member.status == "active"
        ).order_by(Member.name).all()
    else:
        members = db.query(Member).filter(Member.status == "active").order_by(Member.name).all()

    def _m(m):
        sas = [sa for sa in m.spot_assignments
               if sa.is_active and (cycle_id is None or sa.cycle_id == cycle_id)]
        return {
            "id": m.id, "name": m.name, "phone": m.phone,
            "spots": [sa.spot.number for sa in sas if sa.spot],
            "share": sas[0].share if sas else "full",
        }
    return [_m(m) for m in members]


@router.get("/sellers")
def sellers_list(cycle_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Members who have a drawn/received spot (eligible to sell)."""
    if not cycle_id:
        active = db.query(Cycle).filter(Cycle.status == "active").order_by(Cycle.id.desc()).first()
        cycle_id = active.id if active else None
    if cycle_id:
        member_ids = [r[0] for r in db.query(MemberSpot.member_id).filter(
            MemberSpot.cycle_id == cycle_id, MemberSpot.is_active == True
        ).distinct().all()]
        members = db.query(Member).filter(
            Member.id.in_(member_ids)
        ).order_by(Member.name).all()
    else:
        members = db.query(Member).filter(Member.status != "left").order_by(Member.name).all()

    def _m(m):
        sas = [sa for sa in m.spot_assignments
               if sa.is_active and (cycle_id is None or sa.cycle_id == cycle_id)]
        return {
            "id": m.id, "name": m.name, "phone": m.phone,
            "status": m.status,
            "spots": [{"number": sa.spot.number, "status": sa.spot.status}
                      for sa in sas if sa.spot],
        }
    return [_m(m) for m in members]

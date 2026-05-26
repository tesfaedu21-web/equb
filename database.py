import hashlib
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Boolean,
    DateTime, ForeignKey, Text, UniqueConstraint, Index, JSON,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

logger = logging.getLogger("equb.db")


def _utcnow():
    """Naive UTC datetime — avoids deprecated datetime.utcnow()."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

# Load .env file if it exists (for local PostgreSQL development)
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())


class _pwd:
    @staticmethod
    def hash(password: str) -> str:
        salt = secrets.token_hex(16)
        h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260000)
        return f"pbkdf2:{salt}:{h.hex()}"

    @staticmethod
    def verify(password: str, stored: str) -> bool:
        try:
            _, salt, h = stored.split(":", 2)
            return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260000).hex() == h
        except Exception:
            return False

# Use PostgreSQL on Railway (DATABASE_URL is set automatically by Railway Postgres plugin)
# Falls back to local SQLite for development
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./equb.db")

# Railway gives postgres:// but SQLAlchemy needs postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        pool_timeout=30,
    )
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ── Core config ──────────────────────────────────────────────────────────────

class Settings(Base):
    __tablename__ = "settings"
    id = Column(Integer, primary_key=True)
    full_spot_amount = Column(Float, default=21000)
    half_spot_amount = Column(Float, default=10500)
    association_deduction = Column(Float, default=1000)
    total_member_spots = Column(Integer, default=113)
    total_assoc_spots = Column(Integer, default=5)
    group_week_interval = Column(Integer, default=4)
    # Vouchers deducted from winner's pot at disbursement
    full_spot_voucher = Column(Float, default=80)
    half_spot_voucher = Column(Float, default=40)
    # Late payment penalty
    penalty_rate       = Column(Float, default=0)   # % of payment amount; 0 = disabled
    penalty_grace_days = Column(Integer, default=0) # days after draw_date before penalty applies
    # Branding
    group_name = Column(String, default="እቁብ")
    group_tagline = Column(String, default="Equb Manager")
    logo_url = Column(String, nullable=True)
    # Role permissions matrix — JSON dict of {role: {feature: bool}}
    permissions = Column(JSON, nullable=True)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    full_name = Column(String, nullable=False)
    role = Column(String, default="cashier")   # superadmin | admin | cashier
    email = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    totp_secret = Column(String, nullable=True)   # base32 TOTP secret; None = 2FA disabled
    totp_enabled = Column(Boolean, default=False)
    created_at = Column(DateTime, default=_utcnow)


# ── Cycle ────────────────────────────────────────────────────────────────────

class Cycle(Base):
    """One full round of the Equb (covers all spots)."""
    __tablename__ = "cycles"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    start_date = Column(DateTime, nullable=False)
    end_date = Column(DateTime, nullable=True)
    status = Column(String, default="active")             # active, completed
    draw_phase = Column(String, default="collection")     # collection, active
    draw_start_week = Column(Integer, nullable=True)      # week number when admin started draws
    draw_started_at = Column(DateTime, nullable=True)
    notes = Column(Text)
    created_at = Column(DateTime, default=_utcnow)

    # Per-cycle financial settings — set at creation, independent of global Settings
    full_spot_amount     = Column(Float,   nullable=True)
    half_spot_amount     = Column(Float,   nullable=True)
    association_deduction = Column(Float,  nullable=True)
    full_spot_voucher    = Column(Float,   nullable=True)
    half_spot_voucher    = Column(Float,   nullable=True)
    total_member_spots   = Column(Integer, nullable=True)
    total_assoc_spots    = Column(Integer, nullable=True)
    group_week_interval  = Column(Integer, nullable=True)
    frequency            = Column(String, default="weekly")  # weekly | biweekly | monthly
    weeks = relationship("Week", back_populates="cycle", order_by="Week.week_number")
    memberships = relationship("MemberSpot", back_populates="cycle")


# ── Spots ─────────────────────────────────────────────────────────────────────

class Spot(Base):
    """
    A numbered slot in the Equb.
    spot_type = 'member'      → regular member spot
    spot_type = 'association' → owned by the group; profit from its sale → association fund
    """
    __tablename__ = "spots"
    id = Column(Integer, primary_key=True)
    number = Column(Integer, unique=True, nullable=False)
    spot_type = Column(String, default="member")          # member | association
    status = Column(String, default="active")             # active | received

    spot_assignments = relationship("MemberSpot", back_populates="spot")


# ── Members ───────────────────────────────────────────────────────────────────

class Member(Base):
    """A person in the Equb. Can hold one or more spots via MemberSpot."""
    __tablename__ = "members"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    phone = Column(String)
    status = Column(String, default="active")             # active | received | left
    notes = Column(Text)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)
    deleted_at = Column(DateTime, nullable=True)          # set when member leaves/is removed

    spot_assignments = relationship("MemberSpot", back_populates="member",
                                   foreign_keys="MemberSpot.member_id")
    payments = relationship("Payment", back_populates="member")


class MemberSpot(Base):
    """
    Junction: one member can hold many spots; one spot can have 1–2 members (half share).
    Each row = one registration (one spot's worth of obligation for this member).
    cycle_id scopes the membership to a specific cycle — same member can join different cycles.
    """
    __tablename__ = "member_spots"
    __table_args__ = (
        UniqueConstraint("member_id", "spot_id", "cycle_id", name="uq_member_spot_cycle"),
        Index("ix_member_spots_cycle_active", "cycle_id", "is_active"),
    )
    id = Column(Integer, primary_key=True)
    member_id = Column(Integer, ForeignKey("members.id"), nullable=False)
    spot_id = Column(Integer, ForeignKey("spots.id"), nullable=False)
    cycle_id = Column(Integer, ForeignKey("cycles.id"), nullable=True)  # NULL = legacy pre-cycle data
    share = Column(String, default="full")                # full | half
    weekly_contribution = Column(Float, default=21000)    # 21000 full, 10500 half
    is_active = Column(Boolean, default=True)
    exited_at_week_id = Column(Integer, ForeignKey("weeks.id"), nullable=True)
    exit_reason = Column(String, nullable=True)           # left | stopped_paying
    created_at = Column(DateTime, default=_utcnow)

    member = relationship("Member", back_populates="spot_assignments",
                         foreign_keys=[member_id])
    spot = relationship("Spot", back_populates="spot_assignments")
    cycle = relationship("Cycle", back_populates="memberships")


# ── Weeks ─────────────────────────────────────────────────────────────────────

class Week(Base):
    __tablename__ = "weeks"
    __table_args__ = (
        Index("ix_weeks_cycle_status", "cycle_id", "status"),
        UniqueConstraint("cycle_id", "week_number", name="uq_week_cycle_number"),
    )
    id = Column(Integer, primary_key=True)
    cycle_id = Column(Integer, ForeignKey("cycles.id"), nullable=False)
    week_number = Column(Integer, nullable=False)
    draw_date = Column(DateTime, nullable=False)
    is_group_week = Column(Boolean, default=False)        # every Nth week → buyer sale
    is_worker_week = Column(Boolean, default=False)       # extra week for worker/staff payment
    gross_pot = Column(Float, nullable=True)
    association_amount = Column(Float, nullable=True)     # deducted to assoc fund
    net_pot = Column(Float, nullable=True)
    status = Column(String, default="pending")            # pending | drawn | sold
    winner_spot_id = Column(Integer, ForeignKey("spots.id"), nullable=True)
    notes = Column(Text)

    cycle = relationship("Cycle", back_populates="weeks")
    winner_spot = relationship("Spot", foreign_keys=[winner_spot_id])
    payments = relationship("Payment", back_populates="week")
    transactions = relationship("PotTransaction", back_populates="week")


# ── Payments ──────────────────────────────────────────────────────────────────

class PaymentBatch(Base):
    """One physical payment event covering one or more weeks for a single member."""
    __tablename__ = "payment_batches"
    id = Column(Integer, primary_key=True)
    member_id = Column(Integer, ForeignKey("members.id"), nullable=False)
    payment_date = Column(DateTime, nullable=False)
    weeks_paid = Column(Integer, nullable=False, default=1)
    total_amount = Column(Float, nullable=False)
    payment_method = Column(String, default="cash")       # cash | bank_transfer | cheque
    reference = Column(String, nullable=True)
    notes = Column(Text)
    collected_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=_utcnow)

    member = relationship("Member")
    payments = relationship("Payment", back_populates="batch")
    collected_by = relationship("User", foreign_keys=[collected_by_id])


class Payment(Base):
    """Weekly payment record per member (amount = total for all their spots that week)."""
    __tablename__ = "payments"
    __table_args__ = (
        Index("ix_payments_week_status", "week_id", "status"),
        Index("ix_payments_member_status", "member_id", "status"),
        UniqueConstraint("member_id", "week_id", name="uq_payment_member_week"),
    )
    id = Column(Integer, primary_key=True)
    member_id = Column(Integer, ForeignKey("members.id"), nullable=False)
    week_id = Column(Integer, ForeignKey("weeks.id"), nullable=False)
    batch_id = Column(Integer, ForeignKey("payment_batches.id"), nullable=True)
    amount = Column(Float, nullable=False)
    paid_date = Column(DateTime, nullable=True)
    payment_method = Column(String, nullable=True)
    reference = Column(String, nullable=True)
    status = Column(String, default="pending")            # pending | paid | late | missed
    penalty_amount = Column(Float, default=0)             # late-payment penalty in ETB
    notes = Column(Text)
    collected_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    member = relationship("Member", back_populates="payments")
    week = relationship("Week", back_populates="payments")
    batch = relationship("PaymentBatch", back_populates="payments")
    collected_by = relationship("User", foreign_keys=[collected_by_id])


# ── Pot transactions ──────────────────────────────────────────────────────────

class PotTransaction(Base):
    """
    Records a pot sale event.
    transaction_type:
      'group_week_sale'   → group/buyer week; any member buys the pot
      'member_sale'       → winning member sells their pot to another member
      'assoc_spot_sale'   → association-owned spot drawn; sold, profit → assoc fund
    """
    __tablename__ = "pot_transactions"
    id = Column(Integer, primary_key=True)
    week_id = Column(Integer, ForeignKey("weeks.id"), nullable=False)
    transaction_type = Column(String, nullable=False)
    original_winner_id = Column(Integer, ForeignKey("members.id"), nullable=True)
    seller_id = Column(Integer, ForeignKey("members.id"), nullable=True)
    buyer_id = Column(Integer, ForeignKey("members.id"), nullable=False)
    percentage = Column(Float, nullable=True)
    gross_amount = Column(Float, nullable=False)
    seller_fee = Column(Float, nullable=True)              # profit → seller (or assoc fund if assoc_spot_sale)
    buyer_receives = Column(Float, nullable=False)
    transaction_date = Column(DateTime, default=_utcnow)
    notes = Column(Text)

    week = relationship("Week", back_populates="transactions")
    original_winner = relationship("Member", foreign_keys=[original_winner_id])
    seller = relationship("Member", foreign_keys=[seller_id])
    buyer = relationship("Member", foreign_keys=[buyer_id])


# ── Pot Disbursements ────────────────────────────────────────────────────────

class PotDisbursement(Base):
    """
    Records the physical cheque payment made to the draw winner.
    Member brings 3 guarantors the week before; collects cheque the following week.
    """
    __tablename__ = "pot_disbursements"
    id = Column(Integer, primary_key=True)
    week_id = Column(Integer, ForeignKey("weeks.id"), nullable=False)
    winner_spot_id = Column(Integer, ForeignKey("spots.id"), nullable=True)
    member_id = Column(Integer, ForeignKey("members.id"), nullable=True)   # set for half-spot split cheques
    gross_amount = Column(Float, nullable=False)
    voucher_deduction = Column(Float, default=0)
    net_amount = Column(Float, nullable=False)
    service_fee = Column(Float, default=0)                # worker week contribution
    cheque_number = Column(String, nullable=False)
    cheque_date = Column(DateTime, nullable=False)
    guarantor_1_id = Column(Integer, ForeignKey("members.id"), nullable=False)
    guarantor_2_id = Column(Integer, ForeignKey("members.id"), nullable=False)
    guarantor_3_id = Column(Integer, ForeignKey("members.id"), nullable=False)
    status = Column(String, default="issued")             # issued | collected | voided
    voucher_paid = Column(Boolean, default=False)
    voucher_paid_date = Column(DateTime, nullable=True)
    voided_at = Column(DateTime, nullable=True)
    void_reason = Column(Text, nullable=True)
    voided_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    notes = Column(Text)
    created_at = Column(DateTime, default=_utcnow)

    week = relationship("Week")
    winner_spot = relationship("Spot", foreign_keys=[winner_spot_id])
    member = relationship("Member", foreign_keys=[member_id])
    guarantor_1 = relationship("Member", foreign_keys=[guarantor_1_id])
    guarantor_2 = relationship("Member", foreign_keys=[guarantor_2_id])
    guarantor_3 = relationship("Member", foreign_keys=[guarantor_3_id])
    voided_by = relationship("User", foreign_keys=[voided_by_id])


# ── Association Fund ──────────────────────────────────────────────────────────

class AssociationExpense(Base):
    """Expenses deducted from the association fund before returning to members."""
    __tablename__ = "association_expenses"
    id = Column(Integer, primary_key=True)
    cycle_id = Column(Integer, ForeignKey("cycles.id"), nullable=False)
    description = Column(String, nullable=False)   # paper, pen, meeting costs, etc.
    amount = Column(Float, nullable=False)
    expense_date = Column(DateTime, default=_utcnow)
    notes = Column(Text)
    created_at = Column(DateTime, default=_utcnow)

    cycle = relationship("Cycle")


# ── Distribution Cheques ─────────────────────────────────────────────────────

class DistributionCheque(Base):
    """Cheque issued to a member for their end-of-cycle profit distribution share."""
    __tablename__ = "distribution_cheques"
    id = Column(Integer, primary_key=True)
    cycle_id = Column(Integer, ForeignKey("cycles.id"), nullable=False)
    member_id = Column(Integer, ForeignKey("members.id"), nullable=False)
    amount = Column(Float, nullable=False)
    cheque_number = Column(String, nullable=False)
    cheque_date = Column(DateTime, nullable=False)
    status = Column(String, default="issued")   # issued | collected
    collected_at = Column(DateTime, nullable=True)
    notes = Column(Text)
    created_at = Column(DateTime, default=_utcnow)

    cycle = relationship("Cycle")
    member = relationship("Member")


# ── Voucher Returns ───────────────────────────────────────────────────────────

class VoucherReturn(Base):
    """Physical voucher cards returned by vendor per week, recorded manually by admin."""
    __tablename__ = "voucher_returns"
    id = Column(Integer, primary_key=True)
    week_id = Column(Integer, ForeignKey("weeks.id"), unique=True, nullable=False)
    full_count = Column(Integer, default=0, nullable=False)
    half_count = Column(Integer, default=0, nullable=False)
    notes = Column(Text)
    recorded_at = Column(DateTime, default=_utcnow)
    vendor_paid = Column(Boolean, default=False)
    vendor_paid_date = Column(DateTime, nullable=True)

    week = relationship("Week")


# ── Notifications ─────────────────────────────────────────────────────────────

class AuditLog(Base):
    """Immutable record of every create/update/delete action on financial data."""
    __tablename__ = "audit_log"
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=_utcnow, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    username = Column(String, nullable=True)          # denormalized for history
    action = Column(String, nullable=False)           # create | update | delete | void
    table_name = Column(String, nullable=False)
    record_id = Column(Integer, nullable=True)
    description = Column(Text, nullable=False)        # human-readable summary
    old_value = Column(JSON, nullable=True)
    new_value = Column(JSON, nullable=True)

    user = relationship("User", foreign_keys=[user_id])


def log_action(db, *, user, action: str, table: str, record_id=None,
               description: str, old=None, new=None):
    """Append one audit entry. Call inside any endpoint that mutates financial data."""
    db.add(AuditLog(
        user_id=getattr(user, "id", None),
        username=getattr(user, "username", str(user)) if user else "system",
        action=action,
        table_name=table,
        record_id=record_id,
        description=description,
        old_value=old,
        new_value=new,
    ))


class NotificationSettings(Base):
    __tablename__ = "notification_settings"
    id = Column(Integer, primary_key=True)
    provider = Column(String, default="africastalking")
    api_key = Column(String, nullable=True)
    username = Column(String, nullable=True)
    sender_id = Column(String, nullable=True)
    is_active = Column(Boolean, default=False)
    device_token = Column(String, nullable=True)
    sms_language = Column(String, default="en")    # en | am | both


class SmsQueue(Base):
    __tablename__ = "sms_queue"
    id = Column(Integer, primary_key=True)
    phone = Column(String, nullable=False)
    message = Column(Text, nullable=False)
    template_key = Column(String, nullable=True)
    member_id = Column(Integer, ForeignKey("members.id"), nullable=True)
    status = Column(String, default="pending")     # pending | sent | failed | cancelled
    attempts = Column(Integer, default=0)
    created_at = Column(DateTime, default=_utcnow)
    sent_at = Column(DateTime, nullable=True)
    provider_response = Column(Text, nullable=True)

    member = relationship("Member")


class NotificationTemplate(Base):
    __tablename__ = "notification_templates"
    id = Column(Integer, primary_key=True)
    key = Column(String, unique=True, nullable=False)
    title = Column(String, nullable=False)
    message = Column(Text, nullable=False)
    message_am = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)


class NotificationLog(Base):
    __tablename__ = "notification_logs"
    id = Column(Integer, primary_key=True)
    member_id = Column(Integer, ForeignKey("members.id"), nullable=True)
    phone = Column(String, nullable=False)
    template_key = Column(String, nullable=True)
    message = Column(Text, nullable=False)
    status = Column(String, default="pending")
    provider_response = Column(Text, nullable=True)
    batch_id = Column(String, nullable=True)
    sent_at = Column(DateTime, default=_utcnow)

    member = relationship("Member")


# ── Seed data ─────────────────────────────────────────────────────────────────

DEFAULT_TEMPLATES = [
    {
        "key": "payment_confirmed",
        "title": "Payment Confirmed",
        "message": "Dear {member_name}, your Equb payment of {amount} ETB for Week {week_number} ({draw_date}) has been received via {payment_method}. Thank you!",
        "message_am": "ውድ {member_name}፣ የሳምንት {week_number} ({draw_date}) የእቁብ ክፍያዎ {amount} ብር በ{payment_method} ደርሷል። አመሰግናለን!",
    },
    {
        "key": "payment_reminder",
        "title": "Payment Reminder",
        "message": "Dear {member_name}, your Equb payment of {amount} ETB is due for week {week_number} ({draw_date}). Please pay on time. Thank you.",
        "message_am": "ውድ {member_name}፣ የሳምንት {week_number} ({draw_date}) የእቁብ ክፍያ {amount} ብር ይጠብቃል። በጊዜ ይክፈሉ። አመሰግናለን።",
    },
    {
        "key": "missed_payment",
        "title": "Missed Payment Notice",
        "message": "Dear {member_name}, you have {unpaid_count} missed Equb payment(s) totaling {unpaid_amount} ETB (weeks {weeks_list}). Please settle your balance to remain eligible.",
        "message_am": "ውድ {member_name}፣ {unpaid_count} ያልተከፈሉ ክፍያዎች (ሳምንቶች: {weeks_list}) አሉዎት — ድምር {unpaid_amount} ብር። ቀሪ ሂሳብዎን ይጠርጉ።",
    },
    {
        "key": "draw_winner",
        "title": "Draw Winner",
        "message": "Congratulations {member_name}! You won the Equb pot for Week {week_number}. Your pot of {net_pot} ETB will be disbursed after confirming full payment.",
        "message_am": "እንኳን ደስ አለዎ {member_name}! የሳምንት {week_number} የእቁብ ዕጣ ደርሷሎ። {net_pot} ብር ሙሉ ክፍያ ሲረጋገጥ ይከፈልዎታል።",
    },
    {
        "key": "pot_on_hold",
        "title": "Pot On Hold",
        "message": "Dear {member_name}, your pot for Week {week_number} ({net_pot} ETB) is ON HOLD. Please pay {unpaid_count} outstanding week(s) ({unpaid_amount} ETB) to receive it.",
        "message_am": "ውድ {member_name}፣ የሳምንት {week_number} ድርሻዎ ({net_pot} ብር) ታግዷል። {unpaid_count} ያልተከፈሉ ሳምንቶች ({unpaid_amount} ብር) ይፈጽሙ።",
    },
    {
        "key": "pot_sold",
        "title": "Pot Sale",
        "message": "Dear {member_name}, your pot for Week {week_number} was sold. You receive a fee of {seller_fee} ETB. The buyer gets {buyer_receives} ETB.",
        "message_am": "ውድ {member_name}፣ የሳምንት {week_number} ድርሻዎ ተሸጧል። ክፍያዎ {seller_fee} ብር ነው። ገዢው {buyer_receives} ብር ያገኛል።",
    },
    {
        "key": "disbursement_ready",
        "title": "Cheque Ready",
        "message": "Dear {member_name}, your Equb pot cheque for Week {week_number} (Cheque #{cheque_number}) is ready for collection. Please visit the office to sign and collect.",
        "message_am": "ውድ {member_name}፣ የሳምንት {week_number} የእቁብ ቼክዎ (ቼክ #{cheque_number}) ዝግጁ ነው። ለፊርማ እና ለቀብ ቢሮ ይምጡ።",
    },
    {
        "key": "draw_announcement",
        "title": "Draw Announcement",
        "message": "Equb Week {week_number} draw result: Spot #{spot_number} has been drawn. Congratulations to the winner!",
        "message_am": "የሳምንት {week_number} የእቁብ ዕጣ ውጤት: ቁጥር #{spot_number} ወጥቷል። ለአሸናፊው እንኳን ደስ አለዎ!",
    },
]


def _seed_templates(db) -> None:
    for t in DEFAULT_TEMPLATES:
        existing = db.query(NotificationTemplate).filter_by(key=t["key"]).first()
        if not existing:
            db.add(NotificationTemplate(**t))
        elif not existing.message_am and t.get("message_am"):
            existing.message_am = t["message_am"]
    db.commit()


@dataclass
class CycleCfg:
    full_spot_amount: float
    half_spot_amount: float
    association_deduction: float
    full_spot_voucher: float
    half_spot_voucher: float
    total_member_spots: int
    total_assoc_spots: int
    group_week_interval: int


def cycle_cfg(cycle, global_s) -> CycleCfg:
    """Return effective financial settings for a cycle.
    Cycle's own values take precedence; falls back to global settings for legacy cycles."""
    def _pick(cycle_val, global_val, fallback):
        v = cycle_val if cycle_val is not None else global_val
        return v if v is not None else fallback
    cv = cycle
    gs = global_s
    return CycleCfg(
        full_spot_amount     = _pick(getattr(cv, 'full_spot_amount', None),      getattr(gs, 'full_spot_amount', None),      21000),
        half_spot_amount     = _pick(getattr(cv, 'half_spot_amount', None),      getattr(gs, 'half_spot_amount', None),      10500),
        association_deduction= _pick(getattr(cv, 'association_deduction', None), getattr(gs, 'association_deduction', None), 1000),
        full_spot_voucher    = _pick(getattr(cv, 'full_spot_voucher', None),     getattr(gs, 'full_spot_voucher', None),     80),
        half_spot_voucher    = _pick(getattr(cv, 'half_spot_voucher', None),     getattr(gs, 'half_spot_voucher', None),     40),
        total_member_spots   = _pick(getattr(cv, 'total_member_spots', None),    getattr(gs, 'total_member_spots', None),    113),
        total_assoc_spots    = _pick(getattr(cv, 'total_assoc_spots', None),     getattr(gs, 'total_assoc_spots', None),     5),
        group_week_interval  = _pick(getattr(cv, 'group_week_interval', None),   getattr(gs, 'group_week_interval', None),   4),
    )


def _migrate(engine):
    """Add new columns to existing SQLite DB without dropping data."""
    from sqlalchemy import text
    migrations = [
        "ALTER TABLE settings ADD COLUMN full_spot_voucher REAL DEFAULT 80",
        "ALTER TABLE settings ADD COLUMN half_spot_voucher REAL DEFAULT 40",
        "ALTER TABLE settings ADD COLUMN include_worker_slot INTEGER DEFAULT 1",
        "ALTER TABLE settings ADD COLUMN IF NOT EXISTS penalty_rate REAL DEFAULT 0",
        "ALTER TABLE settings ADD COLUMN IF NOT EXISTS penalty_grace_days INTEGER DEFAULT 0",
        "ALTER TABLE weeks ADD COLUMN is_worker_week INTEGER DEFAULT 0",
        "ALTER TABLE pot_disbursements ADD COLUMN service_fee REAL DEFAULT 0",
        # Cycle-scoped memberships: track which cycle each member-spot assignment belongs to
        "ALTER TABLE member_spots ADD COLUMN cycle_id INTEGER REFERENCES cycles(id)",
        # Audit trail: track when records were last modified
        "ALTER TABLE members ADD COLUMN updated_at TIMESTAMP",
        "ALTER TABLE payments ADD COLUMN updated_at TIMESTAMP",
        "ALTER TABLE pot_disbursements ADD COLUMN voucher_paid INTEGER DEFAULT 0",
        "ALTER TABLE pot_disbursements ADD COLUMN voucher_paid_date TIMESTAMP",
        "ALTER TABLE payments ADD COLUMN collected_by_id INTEGER REFERENCES users(id)",
        "ALTER TABLE payment_batches ADD COLUMN collected_by_id INTEGER REFERENCES users(id)",
        # Per-cycle financial settings (null = fall back to global Settings)
        "ALTER TABLE cycles ADD COLUMN full_spot_amount REAL",
        "ALTER TABLE cycles ADD COLUMN half_spot_amount REAL",
        "ALTER TABLE cycles ADD COLUMN association_deduction REAL",
        "ALTER TABLE cycles ADD COLUMN full_spot_voucher REAL",
        "ALTER TABLE cycles ADD COLUMN half_spot_voucher REAL",
        "ALTER TABLE cycles ADD COLUMN total_member_spots INTEGER",
        "ALTER TABLE cycles ADD COLUMN total_assoc_spots INTEGER",
        "ALTER TABLE cycles ADD COLUMN group_week_interval INTEGER",
        "ALTER TABLE cycles ADD COLUMN include_worker_slot INTEGER",
        "ALTER TABLE cycles ADD COLUMN IF NOT EXISTS frequency VARCHAR DEFAULT 'weekly'",
        # Performance indexes
        "CREATE INDEX IF NOT EXISTS ix_weeks_cycle_status ON weeks(cycle_id, status)",
        "CREATE INDEX IF NOT EXISTS ix_payments_week_status ON payments(week_id, status)",
        "CREATE INDEX IF NOT EXISTS ix_payments_member_status ON payments(member_id, status)",
        "CREATE INDEX IF NOT EXISTS ix_member_spots_cycle_active ON member_spots(cycle_id, is_active)",
        "ALTER TABLE member_spots ADD COLUMN IF NOT EXISTS exited_at_week_id INTEGER REFERENCES weeks(id)",
        "ALTER TABLE member_spots ADD COLUMN IF NOT EXISTS exit_reason VARCHAR",
        # Prevent double-assignment of same member to same spot in same cycle
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_member_spot_cycle ON member_spots(member_id, spot_id, cycle_id)",
        # Half-spot disbursement split: allow 2 rows per week (one per half-member)
        "ALTER TABLE pot_disbursements DROP CONSTRAINT IF EXISTS pot_disbursements_week_id_key",
        "ALTER TABLE pot_disbursements ADD COLUMN member_id INTEGER REFERENCES members(id)",
        # Allow sold-without-draw weeks (group week / assoc spot sale) to have no winner_spot_id
        "ALTER TABLE pot_disbursements ALTER COLUMN winner_spot_id DROP NOT NULL",
        # End-of-cycle distribution cheques
        """CREATE TABLE IF NOT EXISTS distribution_cheques (
            id SERIAL PRIMARY KEY,
            cycle_id INTEGER REFERENCES cycles(id) NOT NULL,
            member_id INTEGER REFERENCES members(id) NOT NULL,
            amount REAL NOT NULL,
            cheque_number VARCHAR NOT NULL,
            cheque_date TIMESTAMP NOT NULL,
            status VARCHAR DEFAULT 'issued',
            collected_at TIMESTAMP,
            notes TEXT,
            created_at TIMESTAMP
        )""",
        # Group broadcast SMS sends under a single batch_id for grouped log display
        "ALTER TABLE notification_logs ADD COLUMN batch_id VARCHAR",
        # Physical voucher cards returned by vendor — manually recorded per week
        """CREATE TABLE IF NOT EXISTS voucher_returns (
            id SERIAL PRIMARY KEY,
            week_id INTEGER REFERENCES weeks(id) UNIQUE NOT NULL,
            full_count INTEGER NOT NULL DEFAULT 0,
            half_count INTEGER NOT NULL DEFAULT 0,
            notes TEXT,
            recorded_at TIMESTAMP
        )""",
        "ALTER TABLE voucher_returns ADD COLUMN IF NOT EXISTS vendor_paid BOOLEAN DEFAULT FALSE",
        "ALTER TABLE voucher_returns ADD COLUMN IF NOT EXISTS vendor_paid_date TIMESTAMP",
        # Prevent duplicate week numbers within the same cycle
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_week_cycle_number ON weeks(cycle_id, week_number)",
        # Fix voucher_paid column type: INTEGER→BOOLEAN (must drop default first, then cast, then reset)
        "ALTER TABLE pot_disbursements ALTER COLUMN voucher_paid DROP DEFAULT",
        "ALTER TABLE pot_disbursements ALTER COLUMN voucher_paid TYPE boolean USING CASE WHEN voucher_paid = 0 THEN FALSE ELSE TRUE END",
        "ALTER TABLE pot_disbursements ALTER COLUMN voucher_paid SET DEFAULT FALSE",
        # Soft-delete audit trail: record when a member left or was removed
        "ALTER TABLE members ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP",
        # User email address (optional, for notifications/contact)
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS email VARCHAR",
        # Unique constraint: one payment per member per week
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_payment_member_week ON payments(member_id, week_id)",
        # Late-payment penalty tracking
        "ALTER TABLE payments ADD COLUMN IF NOT EXISTS penalty_amount REAL DEFAULT 0",
        # Disbursement void/correction audit trail
        "ALTER TABLE pot_disbursements ADD COLUMN IF NOT EXISTS voided_at TIMESTAMP",
        "ALTER TABLE pot_disbursements ADD COLUMN IF NOT EXISTS void_reason TEXT",
        "ALTER TABLE pot_disbursements ADD COLUMN IF NOT EXISTS voided_by_id INTEGER REFERENCES users(id)",
        # 2FA: TOTP secret per user (NULL = 2FA disabled)
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_secret VARCHAR",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_enabled BOOLEAN DEFAULT FALSE",
        # Audit log: immutable history of all financial actions
        """CREATE TABLE IF NOT EXISTS audit_log (
            id SERIAL PRIMARY KEY,
            timestamp TIMESTAMP NOT NULL DEFAULT now(),
            user_id INTEGER REFERENCES users(id),
            username VARCHAR,
            action VARCHAR NOT NULL,
            table_name VARCHAR NOT NULL,
            record_id INTEGER,
            description TEXT NOT NULL,
            old_value JSONB,
            new_value JSONB
        )""",
        "CREATE INDEX IF NOT EXISTS ix_audit_log_table_record ON audit_log(table_name, record_id)",
        "CREATE INDEX IF NOT EXISTS ix_audit_log_timestamp ON audit_log(timestamp DESC)",
        # Android SMS gateway: device token + outbound queue
        "ALTER TABLE notification_settings ADD COLUMN IF NOT EXISTS device_token VARCHAR",
        """CREATE TABLE IF NOT EXISTS sms_queue (
            id SERIAL PRIMARY KEY,
            phone VARCHAR NOT NULL,
            message TEXT NOT NULL,
            template_key VARCHAR,
            member_id INTEGER REFERENCES members(id),
            status VARCHAR DEFAULT 'pending',
            attempts INTEGER DEFAULT 0,
            created_at TIMESTAMP,
            sent_at TIMESTAMP,
            provider_response TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS ix_sms_queue_status ON sms_queue(status)",
        # Bilingual templates + language preference
        "ALTER TABLE notification_templates ADD COLUMN IF NOT EXISTS message_am TEXT",
        "ALTER TABLE notification_settings ADD COLUMN IF NOT EXISTS sms_language VARCHAR DEFAULT 'en'",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                conn.rollback()  # PostgreSQL aborts the transaction on error; rollback before next statement


def _backfill_cycle_settings(db):
    """Copy global Settings into any Cycle row that still has NULL settings.
    Runs once on startup; safe to run repeatedly (skips cycles already filled)."""
    gs = db.query(Settings).first()
    if not gs:
        return
    cycles = db.query(Cycle).filter(Cycle.full_spot_amount.is_(None)).all()
    for c in cycles:
        c.full_spot_amount     = gs.full_spot_amount
        c.half_spot_amount     = gs.half_spot_amount
        c.association_deduction = gs.association_deduction
        c.full_spot_voucher    = getattr(gs, 'full_spot_voucher', 80)
        c.half_spot_voucher    = getattr(gs, 'half_spot_voucher', 40)
        c.total_member_spots   = gs.total_member_spots
        c.total_assoc_spots    = gs.total_assoc_spots
        c.group_week_interval  = getattr(gs, 'group_week_interval', 4)
    if cycles:
        db.commit()
        logger.info("Backfilled settings into %d existing cycle(s)", len(cycles))


def init_db():
    Base.metadata.create_all(bind=engine)
    _migrate(engine)
    db = SessionLocal()
    try:
        if not db.query(Settings).first():
            db.add(Settings())
            db.commit()
        if not db.query(NotificationSettings).first():
            db.add(NotificationSettings())
            db.commit()
        _seed_templates(db)
        _backfill_cycle_settings(db)
        # Ensure at least one superadmin exists — promote first admin if none
        if not db.query(User).filter(User.role == "superadmin", User.is_active == True).first():
            first_admin = db.query(User).filter(User.role == "admin").order_by(User.id).first()
            if first_admin:
                first_admin.role = "superadmin"
                db.commit()
                logger.warning("MIGRATION: Promoted user '%s' (id=%d) to superadmin", first_admin.username, first_admin.id)
        if db.query(User).count() == 0:
            import secrets as _sec
            _auto_pw = _sec.token_urlsafe(12)
            db.add(User(username="admin", password_hash=_pwd.hash(_auto_pw),
                        full_name="Administrator", role="superadmin", is_active=True))
            db.commit()
            logger.warning(
                "FIRST RUN: Admin account created — username: admin / password: %s"
                " — change this immediately via Settings → User Accounts", _auto_pw
            )
        if db.query(Spot).count() == 0:
            cfg = db.query(Settings).first()
            total_member = cfg.total_member_spots    # 113
            total_assoc = cfg.total_assoc_spots      # 5
            for i in range(1, total_member + 1):
                db.add(Spot(number=i, spot_type="member"))
            for i in range(total_member + 1, total_member + total_assoc + 1):
                db.add(Spot(number=i, spot_type="association"))
            db.commit()
    finally:
        db.close()

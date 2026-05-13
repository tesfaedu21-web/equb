from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Boolean,
    DateTime, ForeignKey, Text, UniqueConstraint, Index,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from datetime import datetime
import hashlib, secrets, os

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
    engine = create_engine(DATABASE_URL)
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
    # Whether to add one extra worker-payment week at end of cycle
    include_worker_slot = Column(Boolean, default=True)
    # Branding
    group_name = Column(String, default="እቁብ")
    group_tagline = Column(String, default="Equb Manager")
    logo_url = Column(String, nullable=True)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    full_name = Column(String, nullable=False)
    role = Column(String, default="cashier")   # admin | cashier
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


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
    created_at = Column(DateTime, default=datetime.utcnow)

    # Per-cycle financial settings — set at creation, independent of global Settings
    full_spot_amount     = Column(Float,   nullable=True)
    half_spot_amount     = Column(Float,   nullable=True)
    association_deduction = Column(Float,  nullable=True)
    full_spot_voucher    = Column(Float,   nullable=True)
    half_spot_voucher    = Column(Float,   nullable=True)
    total_member_spots   = Column(Integer, nullable=True)
    total_assoc_spots    = Column(Integer, nullable=True)
    group_week_interval  = Column(Integer, nullable=True)
    # include_worker_slot is intentionally NOT mapped here — stored as INTEGER in PG,
    # conflicts with Boolean type. Remains a global Settings flag only.

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
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

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
    created_at = Column(DateTime, default=datetime.utcnow)

    member = relationship("Member", back_populates="spot_assignments",
                         foreign_keys=[member_id])
    spot = relationship("Spot", back_populates="spot_assignments")
    cycle = relationship("Cycle", back_populates="memberships")


# ── Weeks ─────────────────────────────────────────────────────────────────────

class Week(Base):
    __tablename__ = "weeks"
    __table_args__ = (
        Index("ix_weeks_cycle_status", "cycle_id", "status"),
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
    created_at = Column(DateTime, default=datetime.utcnow)

    member = relationship("Member")
    payments = relationship("Payment", back_populates="batch")
    collected_by = relationship("User", foreign_keys=[collected_by_id])


class Payment(Base):
    """Weekly payment record per member (amount = total for all their spots that week)."""
    __tablename__ = "payments"
    __table_args__ = (
        Index("ix_payments_week_status", "week_id", "status"),
        Index("ix_payments_member_status", "member_id", "status"),
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
    notes = Column(Text)
    collected_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

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
    transaction_date = Column(DateTime, default=datetime.utcnow)
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
    week_id = Column(Integer, ForeignKey("weeks.id"), nullable=False, unique=True)
    winner_spot_id = Column(Integer, ForeignKey("spots.id"), nullable=False)
    gross_amount = Column(Float, nullable=False)
    voucher_deduction = Column(Float, default=0)
    net_amount = Column(Float, nullable=False)
    service_fee = Column(Float, default=0)                # worker week contribution
    cheque_number = Column(String, nullable=False)
    cheque_date = Column(DateTime, nullable=False)
    guarantor_1_id = Column(Integer, ForeignKey("members.id"), nullable=False)
    guarantor_2_id = Column(Integer, ForeignKey("members.id"), nullable=False)
    guarantor_3_id = Column(Integer, ForeignKey("members.id"), nullable=False)
    status = Column(String, default="issued")             # issued | collected
    voucher_paid = Column(Boolean, default=False)
    voucher_paid_date = Column(DateTime, nullable=True)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    week = relationship("Week")
    winner_spot = relationship("Spot", foreign_keys=[winner_spot_id])
    guarantor_1 = relationship("Member", foreign_keys=[guarantor_1_id])
    guarantor_2 = relationship("Member", foreign_keys=[guarantor_2_id])
    guarantor_3 = relationship("Member", foreign_keys=[guarantor_3_id])


# ── Association Fund ──────────────────────────────────────────────────────────

class AssociationExpense(Base):
    """Expenses deducted from the association fund before returning to members."""
    __tablename__ = "association_expenses"
    id = Column(Integer, primary_key=True)
    cycle_id = Column(Integer, ForeignKey("cycles.id"), nullable=False)
    description = Column(String, nullable=False)   # paper, pen, meeting costs, etc.
    amount = Column(Float, nullable=False)
    expense_date = Column(DateTime, default=datetime.utcnow)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    cycle = relationship("Cycle")


# ── Notifications ─────────────────────────────────────────────────────────────

class NotificationSettings(Base):
    __tablename__ = "notification_settings"
    id = Column(Integer, primary_key=True)
    provider = Column(String, default="africastalking")
    api_key = Column(String, nullable=True)
    username = Column(String, nullable=True)
    sender_id = Column(String, nullable=True)
    is_active = Column(Boolean, default=False)


class NotificationTemplate(Base):
    __tablename__ = "notification_templates"
    id = Column(Integer, primary_key=True)
    key = Column(String, unique=True, nullable=False)
    title = Column(String, nullable=False)
    message = Column(Text, nullable=False)
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
    sent_at = Column(DateTime, default=datetime.utcnow)

    member = relationship("Member")


# ── Seed data ─────────────────────────────────────────────────────────────────

DEFAULT_TEMPLATES = [
    {
        "key": "payment_confirmed",
        "title": "Payment Confirmed",
        "message": "Dear {member_name}, your Equb payment of {amount} ETB for Week {week_number} ({draw_date}) has been received via {payment_method}. Thank you!",
    },
    {
        "key": "payment_reminder",
        "title": "Payment Reminder",
        "message": "Dear {member_name}, your Equb payment of {amount} ETB is due for week {week_number} ({draw_date}). Please pay on time. Thank you.",
    },
    {
        "key": "missed_payment",
        "title": "Missed Payment Notice",
        "message": "Dear {member_name}, you have {unpaid_count} missed Equb payment(s) totaling {unpaid_amount} ETB (weeks {weeks_list}). Please settle your balance to remain eligible.",
    },
    {
        "key": "draw_winner",
        "title": "Draw Winner",
        "message": "Congratulations {member_name}! You won the Equb pot for Week {week_number}. Your pot of {net_pot} ETB will be disbursed after confirming full payment.",
    },
    {
        "key": "pot_on_hold",
        "title": "Pot On Hold",
        "message": "Dear {member_name}, your pot for Week {week_number} ({net_pot} ETB) is ON HOLD. Please pay {unpaid_count} outstanding week(s) ({unpaid_amount} ETB) to receive it.",
    },
    {
        "key": "pot_sold",
        "title": "Pot Sale",
        "message": "Dear {member_name}, your pot for Week {week_number} was sold. You receive a fee of {seller_fee} ETB. The buyer gets {buyer_receives} ETB.",
    },
    {
        "key": "disbursement_ready",
        "title": "Cheque Ready",
        "message": "Dear {member_name}, your Equb pot cheque for Week {week_number} (Cheque #{cheque_number}) is ready for collection. Please visit the office to sign and collect.",
    },
]


def _seed_templates(db) -> None:
    for t in DEFAULT_TEMPLATES:
        if not db.query(NotificationTemplate).filter_by(key=t["key"]).first():
            db.add(NotificationTemplate(**t))
    db.commit()


def cycle_cfg(cycle, global_s):
    """Return effective financial settings for a cycle.
    Cycle's own values take precedence; falls back to global settings for legacy cycles."""
    def _pick(cycle_val, global_val, fallback):
        v = cycle_val if cycle_val is not None else global_val
        return v if v is not None else fallback
    cv = cycle  # may be None (pre-cycle context)
    gs = global_s
    class Cfg:
        full_spot_amount     = _pick(getattr(cv, 'full_spot_amount', None),     getattr(gs, 'full_spot_amount', None),     21000)
        half_spot_amount     = _pick(getattr(cv, 'half_spot_amount', None),     getattr(gs, 'half_spot_amount', None),     10500)
        association_deduction= _pick(getattr(cv, 'association_deduction', None),getattr(gs, 'association_deduction', None),1000)
        full_spot_voucher    = _pick(getattr(cv, 'full_spot_voucher', None),    getattr(gs, 'full_spot_voucher', None),    80)
        half_spot_voucher    = _pick(getattr(cv, 'half_spot_voucher', None),    getattr(gs, 'half_spot_voucher', None),    40)
        total_member_spots   = _pick(getattr(cv, 'total_member_spots', None),   getattr(gs, 'total_member_spots', None),   113)
        total_assoc_spots    = _pick(getattr(cv, 'total_assoc_spots', None),    getattr(gs, 'total_assoc_spots', None),    5)
        group_week_interval  = _pick(getattr(cv, 'group_week_interval', None),  getattr(gs, 'group_week_interval', None),  4)
        include_worker_slot  = _pick(None,                                       getattr(gs, 'include_worker_slot', None),  True)
    return Cfg()


def _migrate(engine):
    """Add new columns to existing SQLite DB without dropping data."""
    from sqlalchemy import text
    migrations = [
        "ALTER TABLE settings ADD COLUMN full_spot_voucher REAL DEFAULT 80",
        "ALTER TABLE settings ADD COLUMN half_spot_voucher REAL DEFAULT 40",
        "ALTER TABLE settings ADD COLUMN include_worker_slot INTEGER DEFAULT 1",
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
        # Performance indexes
        "CREATE INDEX IF NOT EXISTS ix_weeks_cycle_status ON weeks(cycle_id, status)",
        "CREATE INDEX IF NOT EXISTS ix_payments_week_status ON payments(week_id, status)",
        "CREATE INDEX IF NOT EXISTS ix_payments_member_status ON payments(member_id, status)",
        "CREATE INDEX IF NOT EXISTS ix_member_spots_cycle_active ON member_spots(cycle_id, is_active)",
        # Prevent double-assignment of same member to same spot in same cycle
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_member_spot_cycle ON member_spots(member_id, spot_id, cycle_id)",
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
    cycles = db.query(Cycle).filter(Cycle.full_spot_amount == None).all()
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
        print(f"[init] Backfilled settings into {len(cycles)} existing cycle(s)")


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
        if db.query(User).count() == 0:
            import secrets as _sec
            _auto_pw = _sec.token_urlsafe(12)
            db.add(User(username="admin", password_hash=_pwd.hash(_auto_pw),
                        full_name="Administrator", role="admin", is_active=True))
            db.commit()
            print("\n" + "=" * 60)
            print("[FIRST RUN] Admin account created!")
            print(f"  Username : admin")
            print(f"  Password : {_auto_pw}")
            print("  Change this immediately via Settings → User Accounts")
            print("=" * 60 + "\n")
        if db.query(Spot).count() == 0:
            s = Settings.__new__(Settings)
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

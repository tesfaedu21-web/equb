"""
Shared fixtures for the Equb test suite.

Uses SQLite in-memory (StaticPool) so tests run without a live PostgreSQL
instance. The shared engine is created here and patched onto the database
module so ALL test files — including integration and endpoint tests — use
the same connection.  Patching happens at import time (before any test
module code runs), so there is one canonical SessionLocal for the whole
test run.
"""
import os
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import database as _db_module
from database import Base, Settings, Cycle

# ── Single shared test engine (all integration tests share this DB) ───────────
_SHARED_ENGINE = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
Base.metadata.create_all(_SHARED_ENGINE)
SharedSession = sessionmaker(bind=_SHARED_ENGINE)

# Patch before any test module code runs so login endpoint (which uses
# next(get_db()) directly) sees the same in-memory DB as the Depends path.
_db_module.SessionLocal = SharedSession
_db_module.engine       = _SHARED_ENGINE


# ── Unit-test fixtures (non-integration tests) ────────────────────────────────

@pytest.fixture(scope="session")
def engine():
    return _SHARED_ENGINE


@pytest.fixture
def db():
    session = SharedSession()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def default_settings(db):
    s = Settings()
    db.add(s)
    db.flush()
    return s


@pytest.fixture
def active_cycle(db, default_settings):
    c = Cycle(
        name="Test Cycle",
        status="active",
        full_spot_amount=21000,
        half_spot_amount=10500,
        association_deduction=1000,
        full_spot_voucher=80,
        half_spot_voucher=40,
        total_member_spots=113,
        total_assoc_spots=5,
    )
    db.add(c)
    db.flush()
    return c

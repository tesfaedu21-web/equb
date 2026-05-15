"""
Shared fixtures for the Equb test suite.

Uses SQLite in-memory so tests run without a live PostgreSQL instance.
The DATABASE_URL env var is patched before any app module is imported.
"""
import os
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from database import Base, Settings, Cycle


@pytest.fixture(scope="session")
def engine():
    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(e)
    return e


@pytest.fixture
def db(engine):
    Session = sessionmaker(bind=engine)
    session = Session()
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

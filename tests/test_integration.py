"""
Integration tests: full payment → disbursement flow.

These tests exercise the API layer end-to-end:
  - Real FastAPI middleware (auth, CSRF)
  - Real SQLAlchemy sessions (SQLite in-memory, StaticPool)
  - Real business-logic validation in each endpoint
  - Real net_amount / service_fee computation

Architecture note
─────────────────
The login endpoint calls `next(database.get_db())` directly (not via FastAPI's
Depends), so FastAPI's dependency_overrides cannot intercept it.  We solve this
by patching `database.SessionLocal` at module level with a StaticPool-backed
factory before anything is imported:

  StaticPool ensures every `sessionmaker()` call returns the *same* underlying
  SQLite connection, so seed data committed in the fixture is immediately visible
  to every other session — including the one the login endpoint opens.
"""
import json
import pytest
from datetime import datetime

from fastapi.testclient import TestClient

import database as _db_module
from database import get_db, _pwd, User, NotificationSettings, Cycle, Week, Spot, Member, MemberSpot, Payment

# Use the shared StaticPool engine set up in conftest.py.
# conftest patches _db_module before any test file loads, so these aliases
# always point to the same single in-memory connection.
_IntSession = _db_module.SessionLocal
_INT_ENGINE = _db_module.engine


def _override_get_db():
    db = _IntSession()
    try:
        yield db
    finally:
        db.close()


# ── CSRF header required by csrf_middleware for all /api/ mutations ──────────
_H = {"X-Requested-With": "XMLHttpRequest", "Content-Type": "application/json"}


# ── Module-scoped client (lifespan runs once; cookies persist across tests) ──

@pytest.fixture(scope="module")
def client():
    from main import app
    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()


# ── Seed data (committed once; visible to every request in this module) ──────

@pytest.fixture(scope="module")
def seed(client):  # client dependency ensures lifespan / init_db runs first
    """
    Insert the minimal dataset needed for every integration test:

      Cycle (active, 21000/1000/80, 113+5 spots)
      Spot  #99  (unique number unlikely to collide with spots seeded by init_db)
      4 Members: winner, g1, g2, g3
      MemberSpot: winner → spot #99 (full share)
      Week #99 status=drawn, winner_spot_id=spot#99,  net_pot=2_260_000
      Week #98 status=pending  (for "not drawn" guard test)
      Payment winner/week_99 amount=2_373_000 status=paid
        (covers cash-sufficiency check: collected ≥ gross)
      Admin user "int_admin" / "Admin1234!"
    """
    db = _IntSession()
    try:
        cycle = Cycle(
            name="Integration Cycle",
            status="active",
            start_date=datetime(2024, 1, 1),
            full_spot_amount=21000,
            half_spot_amount=10500,
            association_deduction=1000,
            full_spot_voucher=80,
            half_spot_voucher=40,
            total_member_spots=113,
            total_assoc_spots=5,
        )
        db.add(cycle)
        db.flush()

        spot = Spot(number=200)   # >118 to avoid colliding with init_db seeded spots
        db.add(spot)
        db.flush()

        winner = Member(name="Winner",        phone="+251900000001", status="active")
        g1     = Member(name="Guarantor One", phone="+251900000002", status="active")
        g2     = Member(name="Guarantor Two", phone="+251900000003", status="active")
        g3     = Member(name="Guarantor Three", phone="+251900000004", status="active")
        db.add_all([winner, g1, g2, g3])
        db.flush()

        db.add(MemberSpot(
            member_id=winner.id, spot_id=spot.id, cycle_id=cycle.id,
            share="full", weekly_contribution=21000, is_active=True,
        ))

        week = Week(
            cycle_id=cycle.id, week_number=199,
            draw_date=datetime(2024, 3, 1),
            status="drawn",
            winner_spot_id=spot.id,
            gross_pot=2_373_000, association_amount=113_000, net_pot=2_260_000,
        )
        pending_week = Week(
            cycle_id=cycle.id, week_number=198,
            draw_date=datetime(2024, 2, 22),
            status="pending",
        )
        db.add_all([week, pending_week])
        db.flush()

        # Payment covering the full gross (cash-sufficiency requires collected ≥ gross)
        db.add(Payment(
            member_id=winner.id, week_id=week.id,
            amount=2_373_000, status="paid",
        ))

        # Admin user with known password
        if not db.query(User).filter_by(username="int_admin").first():
            db.add(User(
                username="int_admin",
                password_hash=_pwd.hash("Admin1234!"),
                full_name="Integration Admin",
                role="superadmin",
                is_active=True,
            ))

        # A second payment row (pending) for the payment-marking tests
        extra_week = Week(
            cycle_id=cycle.id, week_number=197,
            draw_date=datetime(2024, 2, 15),
            status="pending",
        )
        db.add(extra_week)
        db.flush()

        pending_pay = Payment(
            member_id=winner.id, week_id=extra_week.id,
            amount=21000, status="pending",
        )
        db.add(pending_pay)
        db.commit()

        return {
            "cycle_id":       cycle.id,
            "week_id":        week.id,
            "pending_week_id": pending_week.id,
            "extra_week_id":  extra_week.id,
            "winner_id":      winner.id,
            "g1_id":          g1.id,
            "g2_id":          g2.id,
            "g3_id":          g3.id,
            "spot_id":        spot.id,
            "pending_pay_id": pending_pay.id,
        }
    finally:
        db.close()


@pytest.fixture(scope="module")
def logged_in(client, seed):
    """Log in as int_admin once; TestClient keeps the session cookie."""
    resp = client.post(
        "/login",
        data={"username": "int_admin", "password": "Admin1234!"},
        follow_redirects=False,
    )
    assert resp.status_code == 302, f"Login failed ({resp.status_code}): {resp.text[:300]}"
    return client


# ═════════════════════════════════════════════════════════════════════════════
# Payment tests
# ═════════════════════════════════════════════════════════════════════════════

class TestPaymentFlow:
    def test_update_payment_to_paid(self, logged_in, seed):
        pid = seed["pending_pay_id"]
        resp = logged_in.put(
            f"/api/payments/{pid}",
            headers=_H,
            content=json.dumps({"status": "paid", "payment_method": "cash"}),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "paid"
        assert body["payment_method"] == "cash"

    def test_update_payment_amount_unchanged(self, logged_in, seed):
        pid = seed["pending_pay_id"]
        resp = logged_in.put(
            f"/api/payments/{pid}",
            headers=_H,
            content=json.dumps({"status": "paid"}),
        )
        assert resp.status_code == 200
        assert resp.json()["amount"] == 21000  # set from MemberSpot.weekly_contribution

    def test_payment_status_cycle_reflects_in_outstanding(self, logged_in, seed):
        """After payment, the member should no longer appear in outstanding-members."""
        resp = logged_in.get(
            f"/api/payments/outstanding-members?cycle_id={seed['cycle_id']}",
            headers=_H,
        )
        assert resp.status_code == 200
        outstanding_ids = [r["member_id"] for r in resp.json()]
        # winner has a paid payment for week_99; only extra_week is pending
        # (that one was marked paid in previous test, so winner should not appear)
        assert seed["winner_id"] not in outstanding_ids


# ═════════════════════════════════════════════════════════════════════════════
# Disbursement tests — ordered so happy-path runs last (creates the row that
# the duplicate-check test then tries to create again)
# ═════════════════════════════════════════════════════════════════════════════

class TestDisbursementFlow:
    def _base_payload(self, seed):
        return {
            "week_id":        seed["week_id"],
            "gross_amount":   2_260_000,
            "service_fee":    0,        # auto-calculated server-side
            "voucher_deduction": 9_440,
            "cheque_number":  "CHQ-001",
            "cheque_date":    "2024-03-08",
            "guarantor_1_id": seed["g1_id"],
            "guarantor_2_id": seed["g2_id"],
            "guarantor_3_id": seed["g3_id"],
        }

    def test_pending_week_blocks_disbursement(self, logged_in, seed):
        payload = self._base_payload(seed)
        payload["week_id"] = seed["pending_week_id"]
        resp = logged_in.post(
            "/api/disbursements",
            headers=_H,
            content=json.dumps(payload),
        )
        assert resp.status_code == 400
        assert "drawn" in resp.json()["detail"].lower()

    def test_duplicate_guarantors_blocked(self, logged_in, seed):
        payload = self._base_payload(seed)
        payload["guarantor_2_id"] = seed["g1_id"]  # g1 == g2 → duplicate
        resp = logged_in.post(
            "/api/disbursements",
            headers=_H,
            content=json.dumps(payload),
        )
        assert resp.status_code == 400
        assert "different" in resp.json()["detail"].lower()

    def test_winner_cannot_be_their_own_guarantor(self, logged_in, seed):
        payload = self._base_payload(seed)
        payload["guarantor_1_id"] = seed["winner_id"]
        resp = logged_in.post(
            "/api/disbursements",
            headers=_H,
            content=json.dumps(payload),
        )
        assert resp.status_code == 400
        assert "winner" in resp.json()["detail"].lower() or "guarantor" in resp.json()["detail"].lower()

    def test_cash_insufficiency_blocks_overdraw(self, logged_in, seed):
        payload = self._base_payload(seed)
        payload["gross_amount"] = 99_000_000  # far more than collected
        resp = logged_in.post(
            "/api/disbursements",
            headers=_H,
            content=json.dumps(payload),
        )
        assert resp.status_code == 400
        assert "insufficient" in resp.json()["detail"].lower()

    def test_happy_path_net_amount_formula(self, logged_in, seed):
        """
        net_amount = gross − service_fee − voucher_deduction
        service_fee is auto-calculated = full_spot_amount = 21,000  (from cycle cfg)
        gross = 2,260,000  voucher = 9,440
        expected net = 2,260,000 − 21,000 − 9,440 = 2,229,560
        """
        payload = self._base_payload(seed)
        resp = logged_in.post(
            "/api/disbursements",
            headers=_H,
            content=json.dumps(payload),
        )
        assert resp.status_code == 200, resp.json()
        body = resp.json()

        assert body["gross_amount"]      == 2_260_000
        assert body["service_fee"]       == 21_000          # auto-calc from cycle
        assert body["voucher_deduction"] == 9_440
        assert body["net_amount"]        == 2_229_560
        assert body["status"]            == "issued"
        assert body["cheque_number"]     == "CHQ-001"

    def test_service_fee_auto_calculated_from_cycle_config(self, logged_in, seed):
        """service_fee in the response equals the cycle's full_spot_amount (21,000)."""
        db = _IntSession()
        try:
            disb = db.execute(
                __import__("sqlalchemy").text(
                    "SELECT service_fee FROM pot_disbursements WHERE week_id=:w"
                ),
                {"w": seed["week_id"]},
            ).fetchone()
            assert disb is not None, "Disbursement should exist after happy-path test"
            assert disb[0] == 21_000
        finally:
            db.close()

    def test_duplicate_disbursement_blocked(self, logged_in, seed):
        """Creating a second disbursement for the same week must fail."""
        payload = self._base_payload(seed)
        payload["cheque_number"] = "CHQ-002"
        resp = logged_in.post(
            "/api/disbursements",
            headers=_H,
            content=json.dumps(payload),
        )
        assert resp.status_code == 400
        assert "already" in resp.json()["detail"].lower()

    def test_disbursement_list_contains_created_record(self, logged_in, seed):
        resp = logged_in.get(
            f"/api/disbursements?cycle_id={seed['cycle_id']}",
            headers=_H,
        )
        assert resp.status_code == 200
        week_ids = [d["week_id"] for d in resp.json()]
        assert seed["week_id"] in week_ids


# ═════════════════════════════════════════════════════════════════════════════
# Half-spot split disbursement
# ═════════════════════════════════════════════════════════════════════════════

class TestHalfSpotDisbursement:
    """
    Two members share spot #100 (half share each).
    Each gets their own cheque (member_id must be specified).
    service_fee for a half-spot winner = half_spot_amount = 10,500.
    """

    @pytest.fixture(scope="class")
    def half_seed(self, logged_in):
        db = _IntSession()
        try:
            cycle = db.query(Cycle).filter_by(status="active").first()

            spot = Spot(number=201)   # >118 to avoid init_db seeded spots
            db.add(spot)
            db.flush()

            m_a = Member(name="Half Winner A", phone="+251900000010", status="active")
            m_b = Member(name="Half Winner B", phone="+251900000011", status="active")
            g1  = Member(name="Half G1",       phone="+251900000012", status="active")
            g2  = Member(name="Half G2",       phone="+251900000013", status="active")
            g3  = Member(name="Half G3",       phone="+251900000014", status="active")
            db.add_all([m_a, m_b, g1, g2, g3])
            db.flush()

            for m in (m_a, m_b):
                db.add(MemberSpot(
                    member_id=m.id, spot_id=spot.id, cycle_id=cycle.id,
                    share="half", weekly_contribution=10500, is_active=True,
                ))

            week = Week(
                cycle_id=cycle.id, week_number=200,
                draw_date=datetime(2024, 4, 1),
                status="drawn",
                winner_spot_id=spot.id,
                gross_pot=2_373_000, association_amount=113_000, net_pot=2_260_000,
            )
            db.add(week)
            db.flush()

            # Pay enough for each member's cheque
            for m in (m_a, m_b):
                db.add(Payment(member_id=m.id, week_id=week.id,
                               amount=1_130_000, status="paid"))
            db.commit()

            return {
                "week_id": week.id,
                "spot_id": spot.id,
                "ma_id": m_a.id, "mb_id": m_b.id,
                "g1_id": g1.id, "g2_id": g2.id, "g3_id": g3.id,
            }
        finally:
            db.close()

    def _cheque(self, seed, member_id, number):
        return {
            "week_id":          seed["week_id"],
            "member_id":        member_id,
            "gross_amount":     1_130_000,
            "service_fee":      0,
            "voucher_deduction": 4_720,       # 118 × 40 (half rate)
            "cheque_number":    number,
            "cheque_date":      "2024-04-08",
            "guarantor_1_id":   seed["g1_id"],
            "guarantor_2_id":   seed["g2_id"],
            "guarantor_3_id":   seed["g3_id"],
        }

    def test_first_half_cheque_service_fee(self, logged_in, half_seed):
        payload = self._cheque(half_seed, half_seed["ma_id"], "HALF-001")
        resp = logged_in.post(
            "/api/disbursements",
            headers=_H,
            content=json.dumps(payload),
        )
        assert resp.status_code == 200, resp.json()
        body = resp.json()
        # service_fee for a half-spot member = half_spot_amount = 10,500
        assert body["service_fee"] == 10_500
        assert body["member_id"]   == half_seed["ma_id"]

    def test_second_half_cheque_allowed(self, logged_in, half_seed):
        """Both half-spot members can each have their own cheque."""
        payload = self._cheque(half_seed, half_seed["mb_id"], "HALF-002")
        resp = logged_in.post(
            "/api/disbursements",
            headers=_H,
            content=json.dumps(payload),
        )
        assert resp.status_code == 200, resp.json()
        assert resp.json()["member_id"] == half_seed["mb_id"]

    def test_third_cheque_for_same_member_blocked(self, logged_in, half_seed):
        """Cannot issue a second cheque to the same half-spot member."""
        payload = self._cheque(half_seed, half_seed["ma_id"], "HALF-003")
        resp = logged_in.post(
            "/api/disbursements",
            headers=_H,
            content=json.dumps(payload),
        )
        assert resp.status_code == 400
        assert "already" in resp.json()["detail"].lower()

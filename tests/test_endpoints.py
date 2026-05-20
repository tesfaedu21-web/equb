"""
Endpoint-level tests: members, draws, settings, and security middleware.

Uses the same StaticPool approach as test_integration.py so all requests
see the same in-memory SQLite database.
"""
import json
import pytest
from datetime import datetime

from fastapi.testclient import TestClient

import database as _db_module
from database import (
    get_db, _pwd,
    User, Settings, Cycle, Week, Spot, Member, MemberSpot,
)

# Use the shared StaticPool engine set up in conftest.py.
_EPSession = _db_module.SessionLocal
_EP_ENGINE = _db_module.engine

_H = {"X-Requested-With": "XMLHttpRequest", "Content-Type": "application/json"}


def _override_get_db():
    db = _EPSession()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(scope="module")
def client():
    from main import app
    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(scope="module")
def seed(client):
    db = _EPSession()
    try:
        # Settings
        if not db.query(Settings).first():
            db.add(Settings(
                full_spot_amount=21000, half_spot_amount=10500,
                association_deduction=1000, full_spot_voucher=80, half_spot_voucher=40,
                total_member_spots=113, total_assoc_spots=5, group_week_interval=4,
                group_name="Test Equb", group_tagline="Testing",
            ))

        # Admin user
        if not db.query(User).filter_by(username="ep_admin").first():
            db.add(User(
                username="ep_admin",
                password_hash=_pwd.hash("Admin1234!"),
                full_name="EP Admin",
                role="superadmin",
                is_active=True,
            ))

        # Active cycle
        cycle = Cycle(
            name="EP Cycle", status="active",
            start_date=datetime(2024, 1, 1),
            full_spot_amount=21000, half_spot_amount=10500,
            association_deduction=1000, full_spot_voucher=80, half_spot_voucher=40,
            total_member_spots=5, total_assoc_spots=1, group_week_interval=4,
        )
        db.add(cycle)
        db.flush()

        # Spots 1-5 (member) + 6 (assoc)
        spots = [Spot(number=i, status="active", spot_type="member") for i in range(300, 306)]
        spots.append(Spot(number=306, status="active", spot_type="association"))
        db.add_all(spots)
        db.flush()

        # Two members
        m1 = Member(name="Alpha Tester", phone="+251900100001", status="active")
        m2 = Member(name="Beta Tester",  phone="+251900100002", status="active")
        db.add_all([m1, m2])
        db.flush()

        db.add(MemberSpot(
            member_id=m1.id, spot_id=spots[0].id,
            cycle_id=cycle.id, share="full", weekly_contribution=21000, is_active=True,
        ))
        db.add(MemberSpot(
            member_id=m2.id, spot_id=spots[1].id,
            cycle_id=cycle.id, share="full", weekly_contribution=21000, is_active=True,
        ))

        # A drawn week
        week = Week(
            cycle_id=cycle.id, week_number=3,
            draw_date=datetime(2024, 1, 15),
            status="drawn", winner_spot_id=spots[0].id,
            gross_pot=105000, association_amount=6000, net_pot=99000,
        )
        db.add(week)
        db.commit()

        return {
            "cycle_id": cycle.id,
            "week_id":  week.id,
            "m1_id":    m1.id,
            "m2_id":    m2.id,
            "spot_ids": [s.id for s in spots],
        }
    finally:
        db.close()


@pytest.fixture(scope="module")
def auth(client, seed):
    """Log in once; TestClient holds the session cookie."""
    resp = client.post(
        "/login",
        data={"username": "ep_admin", "password": "Admin1234!"},
        follow_redirects=False,
    )
    assert resp.status_code == 302, f"Login failed: {resp.text[:200]}"
    return client


# ═════════════════════════════════════════════════════════════════════════════
# CSRF middleware
# ═════════════════════════════════════════════════════════════════════════════

class TestCsrfMiddleware:
    def test_post_without_header_blocked(self, auth, seed):
        resp = auth.post(
            "/api/members",
            content=json.dumps({"name": "CSRF Attacker", "spots": []}),
            headers={"Content-Type": "application/json"},   # no X-Requested-With
        )
        assert resp.status_code == 403
        assert "csrf" in resp.json()["detail"].lower()

    def test_put_without_header_blocked(self, auth, seed):
        resp = auth.put(
            f"/api/members/{seed['m1_id']}",
            content=json.dumps({"name": "Hacker"}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 403

    def test_delete_without_header_blocked(self, auth, seed):
        resp = auth.delete(
            f"/api/members/9999",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 403

    def test_get_requires_no_csrf_header(self, auth, seed):
        resp = auth.get(f"/api/members?cycle_id={seed['cycle_id']}")
        assert resp.status_code == 200


# ═════════════════════════════════════════════════════════════════════════════
# Unauthenticated access
# ═════════════════════════════════════════════════════════════════════════════

class TestAuthRequired:
    def test_api_returns_401_when_not_logged_in(self, client, seed):
        """Fresh client with no session → 401 on API calls."""
        fresh = TestClient(client.app, raise_server_exceptions=True)
        resp = fresh.get("/api/members")
        assert resp.status_code == 401

    def test_html_page_redirects_to_login(self, client):
        fresh = TestClient(client.app, raise_server_exceptions=True)
        resp = fresh.get("/", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["location"]


# ═════════════════════════════════════════════════════════════════════════════
# Member CRUD
# ═════════════════════════════════════════════════════════════════════════════

class TestMemberCrud:
    def test_list_members_returns_seeded(self, auth, seed):
        resp = auth.get(f"/api/members?cycle_id={seed['cycle_id']}")
        assert resp.status_code == 200
        names = [m["name"] for m in resp.json()]
        assert "Alpha Tester" in names
        assert "Beta Tester" in names

    def test_search_by_name(self, auth, seed):
        resp = auth.get(f"/api/members?search=Alpha&cycle_id={seed['cycle_id']}")
        assert resp.status_code == 200
        results = resp.json()
        assert len(results) >= 1
        assert all("alpha" in m["name"].lower() for m in results)

    def test_create_member(self, auth, seed):
        payload = {"name": "Gamma User", "phone": "+251900100099", "spots": [], "notes": "test"}
        resp = auth.post("/api/members", headers=_H, content=json.dumps(payload))
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "Gamma User"
        assert body["status"] == "active"

    def test_create_member_invalid_phone_rejected(self, auth):
        payload = {"name": "Bad Phone", "phone": "not-a-phone", "spots": []}
        resp = auth.post("/api/members", headers=_H, content=json.dumps(payload))
        assert resp.status_code == 422

    def test_create_member_empty_name_rejected(self, auth):
        payload = {"name": "   ", "spots": []}
        resp = auth.post("/api/members", headers=_H, content=json.dumps(payload))
        assert resp.status_code == 422

    def test_update_member_name(self, auth, seed):
        resp = auth.put(
            f"/api/members/{seed['m2_id']}",
            headers=_H,
            content=json.dumps({"name": "Beta Renamed"}),
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Beta Renamed"

    def test_update_nonexistent_member_returns_404(self, auth):
        resp = auth.put(
            "/api/members/99999",
            headers=_H,
            content=json.dumps({"name": "Ghost"}),
        )
        assert resp.status_code == 404

    def test_mark_member_left(self, auth, seed):
        # Create a throwaway member to delete
        db = _EPSession()
        try:
            tmp = Member(name="Temp Leaver", phone="+251900100098", status="active")
            db.add(tmp)
            db.commit()
            tmp_id = tmp.id
        finally:
            db.close()

        resp = auth.delete(f"/api/members/{tmp_id}", headers=_H)
        assert resp.status_code == 200

        # Verify status changed to left
        db = _EPSession()
        try:
            m = db.query(Member).filter_by(id=tmp_id).first()
            assert m.status == "left"
            assert m.deleted_at is not None
        finally:
            db.close()

    def test_member_stats_returns_counts(self, auth, seed):
        resp = auth.get(f"/api/members/stats?cycle_id={seed['cycle_id']}")
        assert resp.status_code == 200
        body = resp.json()
        assert "total" in body
        assert "active" in body
        assert "received" in body
        assert "left" in body
        assert body["total"] >= 2


# ═════════════════════════════════════════════════════════════════════════════
# Draw / cycle endpoints
# ═════════════════════════════════════════════════════════════════════════════

class TestDrawEndpoints:
    def test_list_cycles(self, auth, seed):
        resp = auth.get("/api/draws/cycles")
        assert resp.status_code == 200
        cycle_ids = [c["id"] for c in resp.json()]
        assert seed["cycle_id"] in cycle_ids

    def test_list_weeks(self, auth, seed):
        resp = auth.get(f"/api/draws/cycles/{seed['cycle_id']}/weeks")
        assert resp.status_code == 200
        weeks = resp.json()
        assert len(weeks) >= 1
        week_numbers = [w["week_number"] for w in weeks]
        assert 3 in week_numbers

    def test_week_has_expected_fields(self, auth, seed):
        resp = auth.get(f"/api/draws/cycles/{seed['cycle_id']}/weeks")
        assert resp.status_code == 200
        w = next(x for x in resp.json() if x["week_number"] == 3)
        assert "gross_pot" in w
        assert "net_pot" in w
        assert "status" in w
        assert w["status"] == "drawn"

    def test_active_spots_returns_list(self, auth, seed):
        resp = auth.get(f"/api/draws/active-spots?cycle_id={seed['cycle_id']}")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_cycle_export(self, auth, seed):
        resp = auth.get(f"/api/draws/cycles/{seed['cycle_id']}/export")
        assert resp.status_code == 200
        body = resp.json()
        assert "cycle" in body
        assert "weeks" in body
        assert "members" in body
        assert "payments" in body
        assert body["cycle"]["id"] == seed["cycle_id"]

    def test_cycle_export_nonexistent_returns_404(self, auth):
        resp = auth.get("/api/draws/cycles/99999/export")
        assert resp.status_code == 404

    def test_list_weeks_nonexistent_cycle(self, auth):
        resp = auth.get("/api/draws/cycles/99999/weeks")
        assert resp.status_code == 200
        assert resp.json() == []


# ═════════════════════════════════════════════════════════════════════════════
# Settings
# ═════════════════════════════════════════════════════════════════════════════

class TestSettingsEndpoints:
    def test_get_settings_returns_fields(self, auth):
        resp = auth.get("/api/settings")
        assert resp.status_code == 200
        body = resp.json()
        assert "full_spot_amount" in body
        assert "half_spot_amount" in body
        assert "association_deduction" in body
        assert "include_worker_slot" not in body   # removed from API surface

    def test_update_group_name(self, auth):
        resp = auth.put(
            "/api/settings",
            headers=_H,
            content=json.dumps({"group_name": "Updated Name"}),
        )
        assert resp.status_code == 200
        assert resp.json()["group_name"] == "Updated Name"

    def test_half_spot_cannot_exceed_full_spot(self, auth):
        resp = auth.put(
            "/api/settings",
            headers=_H,
            content=json.dumps({"full_spot_amount": 10000, "half_spot_amount": 10000}),
        )
        assert resp.status_code == 422

    def test_logo_url_must_be_http(self, auth):
        resp = auth.put(
            "/api/settings",
            headers=_H,
            content=json.dumps({"logo_url": "ftp://bad-scheme.com/logo.png"}),
        )
        assert resp.status_code == 422

    def test_logo_url_valid_https_accepted(self, auth):
        resp = auth.put(
            "/api/settings",
            headers=_H,
            content=json.dumps({"logo_url": "https://example.com/logo.png"}),
        )
        assert resp.status_code == 200
        assert resp.json()["logo_url"] == "https://example.com/logo.png"

    def test_logo_url_empty_string_accepted(self, auth):
        # The validator converts "" → None; the field validator returns None cleanly
        resp = auth.put(
            "/api/settings",
            headers=_H,
            content=json.dumps({"logo_url": ""}),
        )
        # exclude_none=True means the None result from validator is excluded from update;
        # the field stays at its previous value (not an error)
        assert resp.status_code == 200

    def test_negative_spot_amount_rejected(self, auth):
        resp = auth.put(
            "/api/settings",
            headers=_H,
            content=json.dumps({"full_spot_amount": -1000}),
        )
        assert resp.status_code == 422


# ═════════════════════════════════════════════════════════════════════════════
# Member search edge cases
# ═════════════════════════════════════════════════════════════════════════════

class TestMemberSearch:
    def test_search_by_spot_number(self, auth, seed):
        resp = auth.get(f"/api/members?search=300&cycle_id={seed['cycle_id']}")
        assert resp.status_code == 200
        # spot 300 is assigned to Alpha Tester
        names = [m["name"] for m in resp.json()]
        assert "Alpha Tester" in names

    def test_search_no_results_returns_empty(self, auth, seed):
        resp = auth.get(f"/api/members?search=ZZZ_NO_MATCH&cycle_id={seed['cycle_id']}")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_search_by_name_partial(self, auth, seed):
        # Non-numeric fragment → hits ILIKE name/phone path
        resp = auth.get("/api/members?search=Alpha")
        assert resp.status_code == 200
        names = [m["name"] for m in resp.json()]
        assert "Alpha Tester" in names

"""
Tests for authentication and password validation logic.
"""
import pytest
from fastapi import HTTPException
from routers.auth import _validate_password


class TestValidatePassword:
    def test_too_short_raises(self):
        with pytest.raises(HTTPException) as exc:
            _validate_password("abc")
        assert exc.value.status_code == 400
        assert "8 characters" in exc.value.detail

    def test_exactly_min_length_allowed(self):
        _validate_password("ValidP8!")   # 8 chars, not all digits, not common

    def test_all_digits_raises(self):
        with pytest.raises(HTTPException) as exc:
            _validate_password("12345678")
        assert exc.value.status_code == 400
        assert "numbers" in exc.value.detail.lower()

    def test_common_password_raises(self):
        with pytest.raises(HTTPException) as exc:
            _validate_password("password")
        assert exc.value.status_code == 400
        assert "common" in exc.value.detail.lower()

    def test_common_password_admin123_raises(self):
        with pytest.raises(HTTPException):
            _validate_password("admin123")

    def test_common_password_equb1234_raises(self):
        with pytest.raises(HTTPException):
            _validate_password("equb1234")

    def test_strong_password_passes(self):
        _validate_password("Str0ng#Pass!")

    def test_mixed_alphanumeric_passes(self):
        _validate_password("Equb2024ok")

    def test_common_check_is_case_insensitive(self):
        with pytest.raises(HTTPException):
            _validate_password("PASSWORD")   # lowercased → "password" which is banned


# ── Rate-limit helpers ────────────────────────────────────────────────────────

class TestRateLimiting:
    def setup_method(self):
        import main
        main._login_attempts.clear()

    def test_fresh_ip_not_limited(self):
        import main
        assert main._is_rate_limited("1.2.3.4") is False

    def test_record_then_check(self):
        import main
        ip = "10.0.0.1"
        for _ in range(main._RATE_MAX - 1):
            main._record_attempt(ip)
        assert main._is_rate_limited(ip) is False

    def test_exceeding_max_triggers_limit(self):
        import main
        ip = "10.0.0.2"
        for _ in range(main._RATE_MAX):
            main._record_attempt(ip)
        assert main._is_rate_limited(ip) is True

    def test_clear_attempts_resets_limit(self):
        import main
        ip = "10.0.0.3"
        for _ in range(main._RATE_MAX):
            main._record_attempt(ip)
        assert main._is_rate_limited(ip) is True
        main._clear_attempts(ip)
        assert main._is_rate_limited(ip) is False

    def test_attempts_outside_window_not_counted(self):
        import time
        import main
        ip = "10.0.0.4"
        # Inject old timestamps outside the 5-minute window
        old_ts = time.time() - main._RATE_WINDOW - 1
        main._login_attempts[ip] = [old_ts] * main._RATE_MAX
        assert main._is_rate_limited(ip) is False

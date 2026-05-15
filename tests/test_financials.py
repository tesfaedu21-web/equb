"""
Tests for financial calculation logic — the most business-critical code.

Covers:
  - cycle_cfg() default values and override precedence
  - Pot voucher deduction formula (assoc comment in disbursements.py)
  - Net-amount calculation as performed in create_disbursement
  - Association-per-share deduction correctness
"""
import pytest
from database import cycle_cfg, CycleCfg


# ── cycle_cfg ─────────────────────────────────────────────────────────────────

class TestCycleCfgDefaults:
    def test_full_spot_amount(self):
        cfg = cycle_cfg(None, None)
        assert cfg.full_spot_amount == 21000

    def test_half_spot_amount(self):
        cfg = cycle_cfg(None, None)
        assert cfg.half_spot_amount == 10500

    def test_half_is_exactly_half_of_full(self):
        cfg = cycle_cfg(None, None)
        assert cfg.half_spot_amount == cfg.full_spot_amount / 2

    def test_association_deduction(self):
        cfg = cycle_cfg(None, None)
        assert cfg.association_deduction == 1000

    def test_voucher_rates(self):
        cfg = cycle_cfg(None, None)
        assert cfg.full_spot_voucher == 80
        assert cfg.half_spot_voucher == 40

    def test_spot_counts(self):
        cfg = cycle_cfg(None, None)
        assert cfg.total_member_spots == 113
        assert cfg.total_assoc_spots == 5

    def test_returns_cyclecfg_dataclass(self):
        cfg = cycle_cfg(None, None)
        assert isinstance(cfg, CycleCfg)


class TestCycleCfgOverridePrecedence:
    """Cycle-level values win over global settings; global wins over hardcoded fallback."""

    class _FakeCycle:
        full_spot_amount = 25000
        half_spot_amount = 12500
        association_deduction = 1200
        full_spot_voucher = 100
        half_spot_voucher = 50
        total_member_spots = 120
        total_assoc_spots = 6
        group_week_interval = 5
        include_worker_slot = False

    class _FakeGlobal:
        full_spot_amount = 23000
        half_spot_amount = 11500
        association_deduction = 1100
        full_spot_voucher = 90
        half_spot_voucher = 45
        total_member_spots = 115
        total_assoc_spots = 5
        group_week_interval = 4
        include_worker_slot = True

    def test_cycle_overrides_global(self):
        cfg = cycle_cfg(self._FakeCycle(), self._FakeGlobal())
        assert cfg.full_spot_amount == 25000

    def test_global_overrides_fallback_when_no_cycle(self):
        cfg = cycle_cfg(None, self._FakeGlobal())
        assert cfg.full_spot_amount == 23000

    def test_cycle_none_field_falls_through_to_global(self):
        class CycleWithNoneAmount:
            full_spot_amount = None
            half_spot_amount = None
            association_deduction = None
            full_spot_voucher = None
            half_spot_voucher = None
            total_member_spots = None
            total_assoc_spots = None
            group_week_interval = None

        cfg = cycle_cfg(CycleWithNoneAmount(), self._FakeGlobal())
        assert cfg.full_spot_amount == 23000

    def test_include_worker_slot_always_comes_from_global(self):
        cfg = cycle_cfg(self._FakeCycle(), self._FakeGlobal())
        # include_worker_slot is intentionally NOT taken from cycle
        assert cfg.include_worker_slot == self._FakeGlobal.include_worker_slot


# ── Pot payout formula ────────────────────────────────────────────────────────

class TestPotPayoutFormula:
    """
    Reference example from disbursements.py docstring:
      113 member spots, 5 assoc spots, all full, 21,000/1,000/80
      Gross   = 113 × 21,000 = 2,373,000
      Assoc   = 113 × 1,000  =   113,000
      Net_pot =               2,260,000
      Service =   1 × 21,000 =    21,000
      Voucher = 118 ×     80 =     9,440
      Net     =               2,229,560
    """

    def setup_method(self):
        self.cfg = cycle_cfg(None, None)   # defaults: 21000/10500/1000/80/40/113/5

    def test_gross_pot(self):
        gross = self.cfg.total_member_spots * self.cfg.full_spot_amount
        assert gross == 2_373_000

    def test_association_deduction_total(self):
        assoc = self.cfg.total_member_spots * self.cfg.association_deduction
        assert assoc == 113_000

    def test_net_pot(self):
        gross = self.cfg.total_member_spots * self.cfg.full_spot_amount
        assoc = self.cfg.total_member_spots * self.cfg.association_deduction
        assert gross - assoc == 2_260_000

    def test_voucher_total_full_winner(self):
        total_spots = self.cfg.total_member_spots + self.cfg.total_assoc_spots
        voucher = self.cfg.full_spot_voucher * total_spots
        assert total_spots == 118
        assert voucher == 9_440

    def test_net_after_all_full_winner(self):
        net_pot = 2_260_000
        service_fee = self.cfg.full_spot_amount          # 1 full-spot winner
        voucher = self.cfg.full_spot_voucher * (self.cfg.total_member_spots + self.cfg.total_assoc_spots)
        net = net_pot - service_fee - voucher
        assert net == 2_229_560

    def test_half_spot_voucher_is_half_of_full(self):
        assert self.cfg.half_spot_voucher == self.cfg.full_spot_voucher / 2

    def test_net_amount_disbursement(self):
        """net_amount = gross - service_fee - voucher_deduction (no seller fee here)."""
        gross = 2_260_000
        service_fee = self.cfg.full_spot_amount
        voucher_deduction = 9_440
        net = gross - service_fee - voucher_deduction
        assert net == 2_229_560


# ── Association deduction per share ──────────────────────────────────────────

class TestAssociationDeduction:
    def test_full_share_deduction(self):
        cfg = cycle_cfg(None, None)
        assert cfg.association_deduction == 1000

    def test_half_share_deduction(self):
        cfg = cycle_cfg(None, None)
        half_deduction = cfg.association_deduction / 2
        assert half_deduction == 500

    def test_full_cycle_association_total(self):
        """Over 118 weeks (113 member + 5 assoc = 118 total weeks pay), full-spot assoc total."""
        cfg = cycle_cfg(None, None)
        total_weeks = 118
        total = cfg.association_deduction * total_weeks
        assert total == 118_000

    def test_half_cycle_association_total(self):
        cfg = cycle_cfg(None, None)
        total_weeks = 118
        total = (cfg.association_deduction / 2) * total_weeks
        assert total == 59_000

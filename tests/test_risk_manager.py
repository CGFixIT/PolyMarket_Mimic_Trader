"""
tests/test_risk_manager.py

Unit tests for RiskManager — range-relative TP/SL logic.

Run: pytest tests/test_risk_manager.py -v
"""

import time
import pytest

from polymarket_copier.core.risk_manager import (
    RiskConfig,
    RiskManager,
    Position,
    ExitReason,
    Side,
    ExposureCapError,
    InvalidPriceError,
)

# ─── Fixtures ─────────────────────────────────────────────────────────────────

BANKROLL = 10_000.0
CFG      = RiskConfig()   # Default config — all tests use this unless overridden


@pytest.fixture
def rm() -> RiskManager:
    # max_trader_allocation=1.0 keeps the per-trader cap from interfering with the
    # market-exposure and threshold tests; the trader cap has dedicated tests in
    # TestTraderAllocationCap below.
    return RiskManager(
        config=RiskConfig(max_trader_allocation=1.0), bankroll=BANKROLL
    )


def build(
    rm:          RiskManager,
    entry:       float,
    market_id:   str   = "mkt_default",
    size:        float = 100.0,
    resolve_ts:  float = None,
) -> Position:
    """Convenience wrapper around build_position for tests."""
    return rm.build_position(
        position_id    = f"pos_{entry}_{market_id}",
        market_id      = market_id,
        token_id       = f"tok_{market_id}",
        trader_address = "0xTRADER",
        entry_price    = entry,
        size_shares    = size,
        resolve_time   = resolve_ts,
    )


# ─── [A] Threshold Computation — Core Math ────────────────────────────────────

class TestThresholdComputation:

    def test_midrange_0_50(self, rm):
        pos = build(rm, 0.50)
        assert abs(pos.tp_price - 0.70)  < 1e-5, f"TP expected 0.70, got {pos.tp_price}"
        assert abs(pos.sl_price - 0.375) < 1e-5, f"SL expected 0.375, got {pos.sl_price}"

    def test_low_entry_0_20(self, rm):
        pos = build(rm, 0.20)
        assert abs(pos.tp_price - 0.52) < 1e-5
        assert abs(pos.sl_price - 0.15) < 1e-5

    def test_high_entry_0_82(self, rm):
        pos = build(rm, 0.82)
        assert abs(pos.tp_price - 0.892) < 1e-5
        assert abs(pos.sl_price - 0.615) < 1e-5
        assert pos.tp_price <= 1.0

    def test_tp_always_above_entry(self, rm):
        for entry in [0.01, 0.10, 0.30, 0.50, 0.70, 0.90, 0.99]:
            pos = build(rm, entry, market_id=f"mkt_{entry}")
            assert pos.tp_price > entry, f"TP must be above entry at {entry}"

    def test_sl_always_below_entry(self, rm):
        for entry in [0.01, 0.10, 0.30, 0.50, 0.70, 0.90, 0.99]:
            pos = build(rm, entry, market_id=f"mkt_{entry}")
            assert pos.sl_price < entry, f"SL must be below entry at {entry}"

    def test_thresholds_always_in_0_1(self, rm):
        for entry in [0.00, 0.01, 0.02, 0.10, 0.50, 0.90, 0.97, 0.99, 1.00]:
            pos = build(rm, entry, market_id=f"mkt_{entry}")
            assert 0.0 <= pos.sl_price <= 1.0, f"SL out of range at entry={entry}"
            assert 0.0 <= pos.tp_price <= 1.0, f"TP out of range at entry={entry}"


# ─── [B] Near-Boundary Edge Cases ─────────────────────────────────────────────

class TestNearBoundaryEntries:

    def test_near_floor_0_02_sl_floored_at_zero(self, rm):
        pos = build(rm, 0.02)
        assert pos.sl_price == 0.0

    def test_near_floor_0_02_tp_well_above_entry(self, rm):
        pos = build(rm, 0.02)
        assert pos.tp_price > 0.02 + CFG.min_tp_abs
        assert abs(pos.tp_price - 0.412) < 1e-5

    def test_entry_0_01(self, rm):
        pos = build(rm, 0.01)
        assert pos.sl_price == 0.0
        assert pos.tp_price > 0.01
        assert pos.tp_price <= 1.0

    def test_entry_exactly_0_00(self, rm):
        pos = build(rm, 0.00)
        assert pos.sl_price == 0.0
        assert abs(pos.tp_price - 0.40) < 1e-5

    def test_near_floor_0_05(self, rm):
        pos = build(rm, 0.05)
        assert abs(pos.sl_price - 0.03) < 1e-5

    def test_near_ceiling_0_97_tp_clamped(self, rm):
        pos = build(rm, 0.97)
        assert pos.tp_price == 1.0

    def test_near_ceiling_0_97_sl_large_downside(self, rm):
        pos = build(rm, 0.97)
        assert abs(pos.sl_price - 0.7275) < 1e-4

    def test_near_ceiling_0_99_tp_clamped(self, rm):
        pos = build(rm, 0.99)
        assert pos.tp_price == 1.0

    def test_entry_exactly_1_00(self, rm):
        pos = build(rm, 1.00)
        assert pos.tp_price == 1.0
        assert abs(pos.sl_price - 0.75) < 1e-5

    def test_near_ceiling_0_95(self, rm):
        pos = build(rm, 0.95)
        assert abs(pos.tp_price - 0.98) < 1e-5


# ─── [C] Invalid Inputs ───────────────────────────────────────────────────────

class TestInvalidInputs:

    def test_price_above_1_raises(self, rm):
        with pytest.raises(InvalidPriceError, match=r"\[0\.0, 1\.0\]"):
            build(rm, 1.01)

    def test_price_below_0_raises(self, rm):
        with pytest.raises(InvalidPriceError, match=r"\[0\.0, 1\.0\]"):
            build(rm, -0.01)

    def test_evaluate_invalid_current_price_raises(self, rm):
        pos = build(rm, 0.50)
        with pytest.raises(InvalidPriceError):
            rm.evaluate(pos, 1.05)

    def test_build_position_zero_bankroll_raises(self):
        with pytest.raises(ValueError, match="positive"):
            RiskManager(config=RiskConfig(), bankroll=0.0)

    def test_direct_position_no_tp_raises(self):
        with pytest.raises(ValueError, match="tp_price"):
            Position(
                position_id="x", market_id="m", token_id="t",
                trader_address="0xA", side=Side.BUY,
                entry_price=0.50, size_shares=100.0,
                tp_price=None, sl_price=0.375,
            )


# ─── [D] Evaluate() — Exit Signal Priority ────────────────────────────────────

class TestEvaluatePriority:

    def test_hold_within_range(self, rm):
        pos = build(rm, 0.50)
        assert rm.evaluate(pos, 0.60) == ExitReason.HOLD

    def test_take_profit_at_tp(self, rm):
        pos = build(rm, 0.50)
        assert rm.evaluate(pos, pos.tp_price) == ExitReason.TAKE_PROFIT

    def test_take_profit_above_tp(self, rm):
        pos = build(rm, 0.50)
        assert rm.evaluate(pos, pos.tp_price + 0.05) == ExitReason.TAKE_PROFIT

    def test_stop_loss_at_sl(self, rm):
        pos = build(rm, 0.50)
        assert rm.evaluate(pos, pos.sl_price) == ExitReason.STOP_LOSS

    def test_stop_loss_below_sl(self, rm):
        pos = build(rm, 0.50)
        assert rm.evaluate(pos, pos.sl_price - 0.05) == ExitReason.STOP_LOSS

    def test_daily_loss_takes_priority_over_tp(self, rm):
        pos = build(rm, 0.50)
        rm._daily_pnl = -(BANKROLL * CFG.daily_loss_limit_pct) - 1.0
        assert rm.evaluate(pos, pos.tp_price) == ExitReason.DAILY_LOSS_LIMIT

    def test_resolution_blackout_overrides_hold(self, rm):
        resolve_soon = time.time() + (12 * 3_600)
        pos = build(rm, 0.50, resolve_ts=resolve_soon)
        assert rm.evaluate(pos, 0.55) == ExitReason.MARKET_RESOLVING

    def test_resolution_blackout_not_triggered_when_far(self, rm):
        resolve_far = time.time() + (72 * 3_600)
        pos = build(rm, 0.50, resolve_ts=resolve_far)
        assert rm.evaluate(pos, 0.55) == ExitReason.HOLD

    def test_resolution_blackout_not_triggered_after_resolution(self, rm):
        resolved_already = time.time() - 3_600
        pos = build(rm, 0.50, resolve_ts=resolved_already)
        assert rm.evaluate(pos, 0.55) == ExitReason.HOLD


# ─── [E] Trailing Stop ────────────────────────────────────────────────────────

class TestTrailingStop:

    def test_peak_updates_on_new_high(self, rm):
        pos = build(rm, 0.50)
        rm.evaluate(pos, 0.65)
        assert pos.peak_price == 0.65

    def test_peak_does_not_update_on_drop(self, rm):
        pos = build(rm, 0.50)
        rm.evaluate(pos, 0.65)
        rm.evaluate(pos, 0.60)
        assert pos.peak_price == 0.65

    def test_trailing_stop_triggers_after_peak(self, rm):
        # Peak must stay below TP (0.70): TP has exit priority, so a peak above
        # TP would already have closed the position via TAKE_PROFIT.
        pos = build(rm, 0.50)
        rm.evaluate(pos, 0.68)   # new high, still below TP
        trail_sl = rm._compute_trail_sl(pos)
        assert trail_sl < pos.tp_price
        assert rm.evaluate(pos, trail_sl - 0.001) == ExitReason.TRAILING_STOP

    def test_trailing_stop_not_triggered_at_entry(self, rm):
        pos = build(rm, 0.50)
        assert rm.evaluate(pos, 0.48) == ExitReason.HOLD

    def test_trailing_sl_never_below_hard_sl(self, rm):
        pos = build(rm, 0.50)
        rm.evaluate(pos, 0.51)
        trail_sl = rm._compute_trail_sl(pos)
        assert trail_sl >= pos.sl_price

    def test_trailing_sl_math_explicit(self, rm):
        pos = build(rm, 0.50)
        rm.evaluate(pos, 0.80)
        expected_trail = 0.80 - ((0.80 - pos.sl_price) * CFG.trailing_stop_fraction)
        assert abs(rm._compute_trail_sl(pos) - expected_trail) < 1e-5

    def test_trailing_stop_not_triggered_above_trail(self, rm):
        # Peak stays below TP (0.70) so TAKE_PROFIT does not pre-empt the check.
        pos = build(rm, 0.50)
        rm.evaluate(pos, 0.68)
        trail_sl = rm._compute_trail_sl(pos)
        assert rm.evaluate(pos, trail_sl + 0.01) == ExitReason.HOLD


# ─── [F] Time Exit ────────────────────────────────────────────────────────────

class TestTimeExit:

    def test_time_exit_triggers_when_stale(self, rm):
        pos = build(rm, 0.50)
        pos.entry_time = time.time() - (50 * 3_600)
        assert rm.evaluate(pos, 0.51) == ExitReason.TIME_EXIT

    def test_time_exit_suppressed_by_large_range_move(self, rm):
        pos = build(rm, 0.50)
        pos.entry_time = time.time() - (50 * 3_600)
        assert rm.evaluate(pos, 0.62) == ExitReason.HOLD

    def test_time_exit_not_triggered_before_threshold(self, rm):
        pos = build(rm, 0.50)
        pos.entry_time = time.time() - (30 * 3_600)
        assert rm.evaluate(pos, 0.51) == ExitReason.HOLD


# ─── [G] Market Exposure Cap ──────────────────────────────────────────────────

class TestMarketExposureCap:

    def test_exposure_cap_enforced(self, rm):
        build(rm, 0.50, market_id="mkt_A", size=1_400.0)  # $700
        with pytest.raises(ExposureCapError, match="cap="):
            build(rm, 0.50, market_id="mkt_A", size=400.0)   # $200 → over cap

    def test_different_markets_independent(self, rm):
        build(rm, 0.50, market_id="mkt_A", size=1_400.0)
        build(rm, 0.50, market_id="mkt_B", size=1_400.0)

    def test_exposure_released_on_exit(self, rm):
        pos = build(rm, 0.50, market_id="mkt_A", size=1_400.0)
        rm.record_exit(pos, exit_price=0.55)
        build(rm, 0.50, market_id="mkt_A", size=1_400.0)

    def test_market_exposure_accessor(self, rm):
        build(rm, 0.50, market_id="mkt_X", size=200.0)  # $100
        assert abs(rm.market_exposure("mkt_X") - 100.0) < 0.01

    def test_market_exposure_cap_accessor(self, rm):
        assert abs(rm.market_exposure_cap() - 800.0) < 0.01


# ─── [H] Record Exit & Bankroll ───────────────────────────────────────────────

class TestRecordExit:

    def test_profitable_exit_increases_bankroll(self, rm):
        pos = build(rm, 0.50, size=1_000.0)
        rm.record_exit(pos, 0.65)
        assert rm.bankroll > BANKROLL
        assert abs(rm.bankroll - (BANKROLL + 150.0)) < 0.01

    def test_losing_exit_decreases_bankroll(self, rm):
        pos = build(rm, 0.50, size=1_000.0)
        rm.record_exit(pos, 0.40)
        assert rm.bankroll < BANKROLL
        assert abs(rm.bankroll - (BANKROLL - 100.0)) < 0.01

    def test_daily_pnl_tracked(self, rm):
        pos = build(rm, 0.50, size=1_000.0)
        rm.record_exit(pos, 0.60)
        assert abs(rm.daily_pnl() - 100.0) < 0.01

    def test_pnl_at_helper(self, rm):
        pos = build(rm, 0.50, size=500.0)
        assert abs(pos.pnl_at(0.65) - 75.0) < 0.01
        assert abs(pos.pnl_at(0.40) - (-50.0)) < 0.01


# ─── [I] Daily Loss Circuit Breaker ───────────────────────────────────────────

class TestDailyLossCircuitBreaker:

    def test_daily_loss_limit_triggers(self, rm):
        pos = build(rm, 0.50)
        rm._daily_pnl = -(BANKROLL * CFG.daily_loss_limit_pct) - 0.01
        assert rm.evaluate(pos, 0.60) == ExitReason.DAILY_LOSS_LIMIT

    def test_daily_loss_just_below_limit_does_not_trigger(self, rm):
        pos = build(rm, 0.50)
        rm._daily_pnl = -(BANKROLL * CFG.daily_loss_limit_pct) + 1.0
        assert rm.evaluate(pos, 0.60) == ExitReason.HOLD

    def test_record_exit_loss_updates_daily_pnl(self, rm):
        pos = build(rm, 0.50, size=1_000.0)
        rm.record_exit(pos, 0.45)
        assert abs(rm.daily_pnl() - (-50.0)) < 0.01

    def test_custom_tight_daily_limit(self):
        cfg = RiskConfig(daily_loss_limit_pct=0.005)
        rm  = RiskManager(config=cfg, bankroll=1_000.0)
        pos = rm.build_position(
            position_id="p1", market_id="m1", token_id="t1",
            trader_address="0xA", entry_price=0.50, size_shares=100.0
        )
        rm._daily_pnl = -(1_000.0 * 0.005) - 0.01
        assert rm.evaluate(pos, 0.55) == ExitReason.DAILY_LOSS_LIMIT


# ─── [J] Resolution Blackout ──────────────────────────────────────────────────

class TestResolutionBlackout:

    def test_blackout_within_24h(self, rm):
        resolve_ts = time.time() + (6 * 3_600)
        pos = build(rm, 0.50, resolve_ts=resolve_ts)
        assert rm.evaluate(pos, 0.55) == ExitReason.MARKET_RESOLVING

    def test_blackout_exactly_at_boundary(self, rm):
        resolve_ts = time.time() + (23 * 3_600 + 59 * 60)
        pos = build(rm, 0.50, resolve_ts=resolve_ts)
        assert rm.evaluate(pos, 0.55) == ExitReason.MARKET_RESOLVING

    def test_no_blackout_25h_out(self, rm):
        resolve_ts = time.time() + (25 * 3_600)
        pos = build(rm, 0.50, resolve_ts=resolve_ts)
        assert rm.evaluate(pos, 0.55) == ExitReason.HOLD

    def test_no_blackout_no_resolve_time(self, rm):
        pos = build(rm, 0.50, resolve_ts=None)
        assert rm.evaluate(pos, 0.55) == ExitReason.HOLD

    def test_custom_blackout_window(self):
        cfg = RiskConfig(resolution_blackout_hours=48.0)
        rm  = RiskManager(config=cfg, bankroll=BANKROLL)
        resolve_ts = time.time() + (36 * 3_600)
        pos = rm.build_position(
            "p1", "m1", "t1", "0xA",
            entry_price=0.50, size_shares=100.0, resolve_time=resolve_ts
        )
        assert rm.evaluate(pos, 0.55) == ExitReason.MARKET_RESOLVING


# ─── [I] Per-Trader Allocation Cap ────────────────────────────────────────────

class TestTraderAllocationCap:
    """max_trader_allocation caps total $ copied from any single trader."""

    def _rm(self, pct=0.05):
        return RiskManager(
            config=RiskConfig(max_trader_allocation=pct), bankroll=BANKROLL
        )

    def test_cap_enforced_for_one_trader(self):
        rm = self._rm(0.05)   # cap = 5% * 10k = $500
        rm.build_position("p1", "mkt_A", "t1", "0xWHALE",
                          entry_price=0.50, size_shares=800.0)   # $400
        with pytest.raises(ExposureCapError, match="Trader"):
            rm.build_position("p2", "mkt_B", "t2", "0xWHALE",
                              entry_price=0.50, size_shares=400.0)  # +$200 → $600 > $500

    def test_different_traders_independent(self):
        rm = self._rm(0.05)
        rm.build_position("p1", "mkt_A", "t1", "0xWHALE",
                          entry_price=0.50, size_shares=900.0)   # $450
        # A different trader has its own independent cap.
        rm.build_position("p2", "mkt_B", "t2", "0xOTHER",
                          entry_price=0.50, size_shares=900.0)   # $450
        assert rm.trader_exposure("0xWHALE") == pytest.approx(450.0)
        assert rm.trader_exposure("0xOTHER") == pytest.approx(450.0)

    def test_exposure_released_on_exit(self):
        rm = self._rm(0.05)
        pos = rm.build_position("p1", "mkt_A", "t1", "0xWHALE",
                                entry_price=0.50, size_shares=900.0)  # $450
        rm.record_exit(pos, 0.50)
        assert rm.trader_exposure("0xWHALE") == pytest.approx(0.0)

    def test_release_exposure_frees_trader_allocation(self):
        rm = self._rm(0.05)
        rm.build_position("p1", "mkt_A", "t1", "0xWHALE",
                          entry_price=0.50, size_shares=900.0)  # $450
        rm.release_exposure("mkt_A", 450.0, "0xWHALE")
        assert rm.trader_exposure("0xWHALE") == pytest.approx(0.0)
        # And the trader can be copied again afterwards.
        rm.build_position("p2", "mkt_A", "t1", "0xWHALE",
                          entry_price=0.50, size_shares=900.0)


# ─── [J] Trading Halt: daily-loss breaker + post-loss cooldown ────────────────

class TestTradingHalt:

    def test_not_halted_by_default(self, rm):
        assert rm.is_trading_halted() is None

    def test_halted_after_daily_loss_limit(self):
        rm = RiskManager(
            config=RiskConfig(
                daily_loss_limit_pct=0.03,
                max_market_exposure_pct=1.0,   # keep market/trader caps out of the
                max_trader_allocation=1.0,     # way so we can drive the loss
            ),
            bankroll=BANKROLL,
        )
        # Drive daily PnL below -3% * 10k = -$300 via a losing exit.
        pos = rm.build_position("p1", "mkt_A", "t1", "0xA",
                                entry_price=0.50, size_shares=4_000.0)  # $2000 notional
        rm.record_exit(pos, 0.40)   # -0.10 * 4000 = -$400 < -$300
        reason = rm.is_trading_halted()
        assert reason is not None
        assert "daily loss" in reason

    def test_cooldown_engages_after_consecutive_losses(self):
        rm = RiskManager(
            config=RiskConfig(
                cooldown_after_losses=3, cooldown_minutes=60,
                daily_loss_limit_pct=1.0,  # keep daily breaker out of the way
            ),
            bankroll=BANKROLL,
        )
        for i in range(3):
            pos = rm.build_position(f"p{i}", "mkt_A", f"t{i}", "0xA",
                                    entry_price=0.50, size_shares=100.0)
            rm.record_exit(pos, 0.49)   # small loss
        reason = rm.is_trading_halted()
        assert reason is not None
        assert "cooldown" in reason

    def test_win_resets_loss_streak(self):
        rm = RiskManager(
            config=RiskConfig(
                cooldown_after_losses=3, cooldown_minutes=60, daily_loss_limit_pct=1.0,
            ),
            bankroll=BANKROLL,
        )
        for i in range(2):
            pos = rm.build_position(f"p{i}", "mkt_A", f"t{i}", "0xA",
                                    entry_price=0.50, size_shares=100.0)
            rm.record_exit(pos, 0.49)   # two losses
        win = rm.build_position("pw", "mkt_A", "tw", "0xA",
                                entry_price=0.50, size_shares=100.0)
        rm.record_exit(win, 0.60)       # a win resets the streak
        loss = rm.build_position("pl", "mkt_A", "tl", "0xA",
                                 entry_price=0.50, size_shares=100.0)
        rm.record_exit(loss, 0.49)      # one more loss — streak is 1, not 3
        assert rm.is_trading_halted() is None

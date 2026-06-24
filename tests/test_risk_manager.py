"""
tests/test_risk_manager.py

Unit tests for RiskManager — range-relative TP/SL logic.

Run: pytest tests/test_risk_manager.py -v
"""

import time
from datetime import timezone, datetime
from decimal import Decimal
import pytest

from polymarket_copier.core.risk_manager import (
    RiskConfig,
    RiskManager,
    Position,
    ExitReason,
    Side,
    ExposureCapError,
    InvalidPriceError,
    _midnight_utc,
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


async def build(
    rm:          RiskManager,
    entry:       float,
    market_id:   str   = "mkt_default",
    size:        float = 100.0,
    resolve_ts:  float = None,
) -> Position:
    """Convenience wrapper around build_position for tests."""
    return await rm.build_position(
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

    @pytest.mark.asyncio
    async def test_midrange_0_50(self, rm):
        pos = await build(rm, 0.50)
        assert abs(pos.tp_price - 0.70)  < 1e-5, f"TP expected 0.70, got {pos.tp_price}"
        assert abs(pos.sl_price - 0.375) < 1e-5, f"SL expected 0.375, got {pos.sl_price}"

    @pytest.mark.asyncio
    async def test_low_entry_0_20(self, rm):
        pos = await build(rm, 0.20)
        assert abs(pos.tp_price - 0.52) < 1e-5
        assert abs(pos.sl_price - 0.15) < 1e-5

    @pytest.mark.asyncio
    async def test_high_entry_0_82(self, rm):
        # H2: min_reward_risk=1.0 caps SL distance to the TP distance.
        # tp_dist = 0.892 - 0.82 = 0.072; raw sl_dist = 0.205 → capped to 0.072.
        # SL = 0.82 - 0.072 = 0.748  (was 0.615 before the R:R floor fix).
        pos = await build(rm, 0.82)
        assert abs(pos.tp_price - 0.892) < 1e-5
        assert abs(pos.sl_price - 0.748) < 1e-5
        assert pos.tp_price <= 1.0

    @pytest.mark.asyncio
    async def test_tp_always_above_entry(self, rm):
        for entry in [0.01, 0.10, 0.30, 0.50, 0.70, 0.90, 0.99]:
            pos = await build(rm, entry, market_id=f"mkt_{entry}")
            assert pos.tp_price > entry, f"TP must be above entry at {entry}"

    @pytest.mark.asyncio
    async def test_sl_always_below_entry(self, rm):
        for entry in [0.01, 0.10, 0.30, 0.50, 0.70, 0.90, 0.99]:
            pos = await build(rm, entry, market_id=f"mkt_{entry}")
            assert pos.sl_price < entry, f"SL must be below entry at {entry}"

    @pytest.mark.asyncio
    async def test_thresholds_always_in_0_1(self, rm):
        for entry in [0.00, 0.01, 0.02, 0.10, 0.50, 0.90, 0.97, 0.99, 1.00]:
            pos = await build(rm, entry, market_id=f"mkt_{entry}")
            assert 0.0 <= pos.sl_price <= 1.0, f"SL out of range at entry={entry}"
            assert 0.0 <= pos.tp_price <= 1.0, f"TP out of range at entry={entry}"


# ─── [B] Near-Boundary Edge Cases ─────────────────────────────────────────────

class TestNearBoundaryEntries:

    @pytest.mark.asyncio
    async def test_near_floor_0_02_sl_floored_at_zero(self, rm):
        pos = await build(rm, 0.02)
        assert pos.sl_price == 0.0

    @pytest.mark.asyncio
    async def test_near_floor_0_02_tp_well_above_entry(self, rm):
        # L5: TP fraction tapers down at low entries. At entry=0.02, threshold=0.20:
        #   t = 0.02/0.20 = 0.10
        #   tp_fraction = 0.25 + (0.40-0.25)*0.10 = 0.265
        #   tp_raw = 0.02 + max(0.98*0.265, 0.03) = 0.02 + 0.2597 = 0.2797
        # (was 0.412 before L5 with flat 0.40 fraction)
        pos = await build(rm, 0.02)
        assert pos.tp_price > 0.02 + CFG.min_tp_abs
        assert abs(pos.tp_price - 0.2797) < 1e-4

    @pytest.mark.asyncio
    async def test_entry_0_01(self, rm):
        pos = await build(rm, 0.01)
        assert pos.sl_price == 0.0
        assert pos.tp_price > 0.01
        assert pos.tp_price <= 1.0

    @pytest.mark.asyncio
    async def test_entry_exactly_0_00(self, rm):
        # L5: at entry=0.00 the TP fraction tapers fully to low_entry_tp_fraction=0.25:
        #   t = 0.00/0.20 = 0.0 → tp_fraction = 0.25
        #   tp_raw = 0.0 + max(1.0*0.25, 0.03) = 0.25  (was 0.40 before L5)
        pos = await build(rm, 0.00)
        assert pos.sl_price == 0.0
        assert abs(pos.tp_price - 0.25) < 1e-5

    @pytest.mark.asyncio
    async def test_near_floor_0_05(self, rm):
        pos = await build(rm, 0.05)
        assert abs(pos.sl_price - 0.03) < 1e-5

    @pytest.mark.asyncio
    async def test_near_ceiling_0_97_tp_clamped(self, rm):
        pos = await build(rm, 0.97)
        assert pos.tp_price == 1.0

    @pytest.mark.asyncio
    async def test_near_ceiling_0_97_sl_large_downside(self, rm):
        # H2: tp_dist=0.03 (clamped to 1.0), max_sl_dist=0.03/1.0=0.03 → SL=0.94 (was 0.7275)
        pos = await build(rm, 0.97)
        assert abs(pos.sl_price - 0.94) < 1e-4

    @pytest.mark.asyncio
    async def test_near_ceiling_0_99_tp_clamped(self, rm):
        pos = await build(rm, 0.99)
        assert pos.tp_price == 1.0

    @pytest.mark.asyncio
    async def test_entry_exactly_1_00(self, rm):
        # H2: tp_raw=1.03→clamped 1.0, tp_dist computed pre-clamp=0.03 → SL=0.97 (was 0.75)
        pos = await build(rm, 1.00)
        assert pos.tp_price == 1.0
        assert abs(pos.sl_price - 0.97) < 1e-5

    @pytest.mark.asyncio
    async def test_near_ceiling_0_95(self, rm):
        pos = await build(rm, 0.95)
        assert abs(pos.tp_price - 0.98) < 1e-5


# ─── [B2] L5: Low-Entry TP Taper ──────────────────────────────────────────────

class TestLowEntryTpTaper:
    """L5: below low_entry_threshold the TP fraction tapers down so the profit
    target stays realistic instead of demanding a multi-hundred-percent move."""

    @pytest.mark.asyncio
    async def test_threshold_boundary_uses_full_fraction(self, rm):
        # At exactly low_entry_threshold (0.20) the taper does NOT apply (exclusive).
        # tp = 0.20 + 0.80*0.40 = 0.52 (unchanged from pre-L5 behavior).
        pos = await build(rm, 0.20)
        assert abs(pos.tp_price - 0.52) < 1e-5

    @pytest.mark.asyncio
    async def test_low_entry_010_tapers_tp(self, rm):
        # entry=0.10, t=0.10/0.20=0.5 → tp_fraction=0.25+(0.40-0.25)*0.5=0.325
        # tp_raw = 0.10 + max(0.90*0.325, 0.03) = 0.10 + 0.2925 = 0.3925
        # (was 0.10 + 0.90*0.40 = 0.46 before L5)
        pos = await build(rm, 0.10)
        assert abs(pos.tp_price - 0.3925) < 1e-4

    @pytest.mark.asyncio
    async def test_taper_produces_lower_tp_than_flat_fraction(self, rm):
        # For any entry below threshold, the tapered TP must be strictly closer
        # to entry than the old flat-0.40 fraction would have produced.
        for entry in [0.02, 0.05, 0.10, 0.15, 0.19]:
            pos = await build(rm, entry, market_id=f"mkt_{entry}")
            flat_tp = entry + (1.0 - entry) * 0.40
            assert pos.tp_price < flat_tp, f"taper failed to lower TP at entry={entry}"
            assert pos.tp_price > entry, f"TP must still exceed entry at {entry}"

    @pytest.mark.asyncio
    async def test_taper_disabled_when_threshold_zero(self):
        # low_entry_threshold=0 disables the taper → full fraction at all entries.
        rm0 = RiskManager(
            config=RiskConfig(max_trader_allocation=1.0, low_entry_threshold=0.0),
            bankroll=BANKROLL,
        )
        pos = await build(rm0, 0.10)
        # tp = 0.10 + 0.90*0.40 = 0.46 (full fraction, no taper)
        assert abs(pos.tp_price - 0.46) < 1e-5


# ─── [C] Invalid Inputs ───────────────────────────────────────────────────────

class TestInvalidInputs:

    @pytest.mark.asyncio
    async def test_price_above_1_raises(self, rm):
        with pytest.raises(InvalidPriceError, match=r"\[0\.0, 1\.0\]"):
            await build(rm, 1.01)

    @pytest.mark.asyncio
    async def test_price_below_0_raises(self, rm):
        with pytest.raises(InvalidPriceError, match=r"\[0\.0, 1\.0\]"):
            await build(rm, -0.01)

    @pytest.mark.asyncio
    async def test_evaluate_invalid_current_price_raises(self, rm):
        pos = await build(rm, 0.50)
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

    @pytest.mark.asyncio
    async def test_hold_within_range(self, rm):
        pos = await build(rm, 0.50)
        assert rm.evaluate(pos, 0.60) == ExitReason.HOLD

    @pytest.mark.asyncio
    async def test_take_profit_at_tp(self, rm):
        pos = await build(rm, 0.50)
        assert rm.evaluate(pos, pos.tp_price) == ExitReason.TAKE_PROFIT

    @pytest.mark.asyncio
    async def test_take_profit_above_tp(self, rm):
        pos = await build(rm, 0.50)
        assert rm.evaluate(pos, pos.tp_price + 0.05) == ExitReason.TAKE_PROFIT

    @pytest.mark.asyncio
    async def test_stop_loss_at_sl(self, rm):
        pos = await build(rm, 0.50)
        assert rm.evaluate(pos, pos.sl_price) == ExitReason.STOP_LOSS

    @pytest.mark.asyncio
    async def test_stop_loss_below_sl(self, rm):
        pos = await build(rm, 0.50)
        assert rm.evaluate(pos, pos.sl_price - 0.05) == ExitReason.STOP_LOSS

    @pytest.mark.asyncio
    async def test_daily_loss_takes_priority_over_tp(self, rm):
        pos = await build(rm, 0.50)
        rm._daily_pnl = Decimal(str(-(BANKROLL * CFG.daily_loss_limit_pct) - 1.0))
        assert rm.evaluate(pos, pos.tp_price) == ExitReason.DAILY_LOSS_LIMIT

    @pytest.mark.asyncio
    async def test_resolution_blackout_overrides_hold(self, rm):
        resolve_soon = time.time() + (12 * 3_600)
        pos = await build(rm, 0.50, resolve_ts=resolve_soon)
        assert rm.evaluate(pos, 0.55) == ExitReason.MARKET_RESOLVING

    @pytest.mark.asyncio
    async def test_resolution_blackout_not_triggered_when_far(self, rm):
        resolve_far = time.time() + (72 * 3_600)
        pos = await build(rm, 0.50, resolve_ts=resolve_far)
        assert rm.evaluate(pos, 0.55) == ExitReason.HOLD

    @pytest.mark.asyncio
    async def test_resolution_blackout_not_triggered_after_resolution(self, rm):
        resolved_already = time.time() - 3_600
        pos = await build(rm, 0.50, resolve_ts=resolved_already)
        assert rm.evaluate(pos, 0.55) == ExitReason.HOLD


# ─── [E] Trailing Stop ────────────────────────────────────────────────────────

class TestTrailingStop:

    @pytest.mark.asyncio
    async def test_peak_updates_on_new_high(self, rm):
        # evaluate() no longer mutates pos.peak_price; the caller (copier) does.
        # This test verifies that evaluate() uses an effective peak internally
        # and that the caller is responsible for persisting the new peak.
        pos = await build(rm, 0.50)
        rm.evaluate(pos, 0.65)
        # pos.peak_price is still at entry — caller must set it explicitly.
        assert pos.peak_price == 0.50
        # Simulate what copier.handle_price_tick does after evaluate():
        pos.peak_price = 0.65
        assert pos.peak_price == 0.65

    @pytest.mark.asyncio
    async def test_peak_does_not_update_on_drop(self, rm):
        pos = await build(rm, 0.50)
        pos.peak_price = 0.65  # caller sets peak after a prior high
        rm.evaluate(pos, 0.60)
        # caller only updates peak when tick.price > pos.peak_price
        assert pos.peak_price == 0.65

    @pytest.mark.asyncio
    async def test_trailing_stop_triggers_after_peak(self, rm):
        # Peak must stay below TP (0.70): TP has exit priority, so a peak above
        # TP would already have closed the position via TAKE_PROFIT.
        pos = await build(rm, 0.50)
        # Simulate caller updating peak after evaluate() returned HOLD at 0.68
        pos.peak_price = 0.68
        trail_sl = rm._compute_trail_sl(pos)
        assert trail_sl < pos.tp_price
        assert rm.evaluate(pos, trail_sl - 0.001) == ExitReason.TRAILING_STOP

    @pytest.mark.asyncio
    async def test_trailing_stop_not_triggered_at_entry(self, rm):
        pos = await build(rm, 0.50)
        assert rm.evaluate(pos, 0.48) == ExitReason.HOLD

    @pytest.mark.asyncio
    async def test_trailing_sl_never_below_hard_sl(self, rm):
        pos = await build(rm, 0.50)
        pos.peak_price = 0.51  # caller sets after a small new high
        trail_sl = rm._compute_trail_sl(pos)
        assert trail_sl >= pos.sl_price

    @pytest.mark.asyncio
    async def test_trailing_sl_math_explicit(self, rm):
        # H1: trail anchors to run-up from entry (peak - entry), not gap to SL.
        # entry=0.50, peak=0.80, fraction=0.40 → trail = 0.80 - 0.30*0.40 = 0.68
        pos = await build(rm, 0.50)
        pos.peak_price = 0.80
        expected_trail = 0.80 - ((0.80 - pos.entry_price) * CFG.trailing_stop_fraction)
        assert abs(rm._compute_trail_sl(pos) - expected_trail) < 1e-5

    @pytest.mark.asyncio
    async def test_trailing_stop_not_triggered_above_trail(self, rm):
        # Peak stays below TP (0.70) so TAKE_PROFIT does not pre-empt the check.
        pos = await build(rm, 0.50)
        pos.peak_price = 0.68  # caller sets after new high
        trail_sl = rm._compute_trail_sl(pos)
        assert rm.evaluate(pos, trail_sl + 0.01) == ExitReason.HOLD


# ─── [F] Time Exit ────────────────────────────────────────────────────────────

class TestTimeExit:

    @pytest.mark.asyncio
    async def test_time_exit_triggers_when_stale(self, rm):
        pos = await build(rm, 0.50)
        pos.entry_time = time.time() - (50 * 3_600)
        assert rm.evaluate(pos, 0.51) == ExitReason.TIME_EXIT

    @pytest.mark.asyncio
    async def test_time_exit_suppressed_by_large_range_move(self, rm):
        pos = await build(rm, 0.50)
        pos.entry_time = time.time() - (50 * 3_600)
        assert rm.evaluate(pos, 0.62) == ExitReason.HOLD

    @pytest.mark.asyncio
    async def test_time_exit_not_triggered_before_threshold(self, rm):
        pos = await build(rm, 0.50)
        pos.entry_time = time.time() - (30 * 3_600)
        assert rm.evaluate(pos, 0.51) == ExitReason.HOLD


# ─── [G] Market Exposure Cap ──────────────────────────────────────────────────

class TestMarketExposureCap:

    @pytest.mark.asyncio
    async def test_exposure_cap_enforced(self, rm):
        await build(rm, 0.50, market_id="mkt_A", size=1_400.0)  # $700
        with pytest.raises(ExposureCapError, match="cap="):
            await build(rm, 0.50, market_id="mkt_A", size=400.0)   # $200 → over cap

    @pytest.mark.asyncio
    async def test_different_markets_independent(self, rm):
        await build(rm, 0.50, market_id="mkt_A", size=1_400.0)
        await build(rm, 0.50, market_id="mkt_B", size=1_400.0)

    @pytest.mark.asyncio
    async def test_exposure_released_on_exit(self, rm):
        pos = await build(rm, 0.50, market_id="mkt_A", size=1_400.0)
        await rm.record_exit(pos, exit_price=0.55)
        await build(rm, 0.50, market_id="mkt_A", size=1_400.0)

    @pytest.mark.asyncio
    async def test_market_exposure_accessor(self, rm):
        await build(rm, 0.50, market_id="mkt_X", size=200.0)  # $100
        assert abs(rm.market_exposure("mkt_X") - 100.0) < 0.01

    def test_market_exposure_cap_accessor(self, rm):
        assert abs(rm.market_exposure_cap() - 800.0) < 0.01


# ─── [H] Record Exit & Bankroll ───────────────────────────────────────────────

class TestRecordExit:

    @pytest.mark.asyncio
    async def test_profitable_exit_increases_bankroll(self, rm):
        pos = await build(rm, 0.50, size=1_000.0)
        await rm.record_exit(pos, 0.65)
        assert rm.bankroll > BANKROLL
        assert abs(rm.bankroll - (BANKROLL + 150.0)) < 0.01

    @pytest.mark.asyncio
    async def test_losing_exit_decreases_bankroll(self, rm):
        pos = await build(rm, 0.50, size=1_000.0)
        await rm.record_exit(pos, 0.40)
        assert rm.bankroll < BANKROLL
        assert abs(rm.bankroll - (BANKROLL - 100.0)) < 0.01

    @pytest.mark.asyncio
    async def test_daily_pnl_tracked(self, rm):
        pos = await build(rm, 0.50, size=1_000.0)
        await rm.record_exit(pos, 0.60)
        assert abs(rm.daily_pnl() - 100.0) < 0.01

    @pytest.mark.asyncio
    async def test_pnl_at_helper(self, rm):
        pos = await build(rm, 0.50, size=500.0)
        assert abs(pos.pnl_at(0.65) - 75.0) < 0.01
        assert abs(pos.pnl_at(0.40) - (-50.0)) < 0.01

    @pytest.mark.asyncio
    async def test_record_exit_resets_stale_daily_window(self, rm):
        # M8: an exit recorded after a UTC-midnight rollover must book PnL into the
        # NEW calendar day, not accumulate onto a stale prior-day window. Simulate a
        # stale window by stamping _day_start_ts two days back and pre-loading a loss.
        rm._daily_pnl = Decimal("-250.0")
        rm._day_start_ts = time.time() - 2 * 86_400  # window belongs to two days ago
        pos = await build(rm, 0.50, size=1_000.0)
        await rm.record_exit(pos, 0.60)   # +100 profit, booked into the fresh window
        # Stale -250 was discarded at the rollover; only today's +100 remains.
        assert abs(rm.daily_pnl() - 100.0) < 0.01


# ─── [I] Daily Loss Circuit Breaker ───────────────────────────────────────────

class TestDailyLossCircuitBreaker:

    @pytest.mark.asyncio
    async def test_daily_loss_limit_triggers(self, rm):
        pos = await build(rm, 0.50)
        rm._daily_pnl = Decimal(str(-(BANKROLL * CFG.daily_loss_limit_pct) - 0.01))
        assert rm.evaluate(pos, 0.60) == ExitReason.DAILY_LOSS_LIMIT

    @pytest.mark.asyncio
    async def test_daily_loss_just_below_limit_does_not_trigger(self, rm):
        pos = await build(rm, 0.50)
        rm._daily_pnl = Decimal(str(-(BANKROLL * CFG.daily_loss_limit_pct) + 1.0))
        assert rm.evaluate(pos, 0.60) == ExitReason.HOLD

    @pytest.mark.asyncio
    async def test_record_exit_loss_updates_daily_pnl(self, rm):
        pos = await build(rm, 0.50, size=1_000.0)
        await rm.record_exit(pos, 0.45)
        assert abs(rm.daily_pnl() - (-50.0)) < 0.01

    @pytest.mark.asyncio
    async def test_custom_tight_daily_limit(self):
        cfg = RiskConfig(daily_loss_limit_pct=0.005)
        rm  = RiskManager(config=cfg, bankroll=1_000.0)
        pos = await rm.build_position(
            position_id="p1", market_id="m1", token_id="t1",
            trader_address="0xA", entry_price=0.50, size_shares=100.0
        )
        rm._daily_pnl = Decimal(str(-(1_000.0 * 0.005) - 0.01))
        assert rm.evaluate(pos, 0.55) == ExitReason.DAILY_LOSS_LIMIT


# ─── [J] Resolution Blackout ──────────────────────────────────────────────────

class TestResolutionBlackout:

    @pytest.mark.asyncio
    async def test_blackout_within_24h(self, rm):
        resolve_ts = time.time() + (6 * 3_600)
        pos = await build(rm, 0.50, resolve_ts=resolve_ts)
        assert rm.evaluate(pos, 0.55) == ExitReason.MARKET_RESOLVING

    @pytest.mark.asyncio
    async def test_blackout_exactly_at_boundary(self, rm):
        resolve_ts = time.time() + (23 * 3_600 + 59 * 60)
        pos = await build(rm, 0.50, resolve_ts=resolve_ts)
        assert rm.evaluate(pos, 0.55) == ExitReason.MARKET_RESOLVING

    @pytest.mark.asyncio
    async def test_no_blackout_25h_out(self, rm):
        resolve_ts = time.time() + (25 * 3_600)
        pos = await build(rm, 0.50, resolve_ts=resolve_ts)
        assert rm.evaluate(pos, 0.55) == ExitReason.HOLD

    @pytest.mark.asyncio
    async def test_no_blackout_no_resolve_time(self, rm):
        pos = await build(rm, 0.50, resolve_ts=None)
        assert rm.evaluate(pos, 0.55) == ExitReason.HOLD

    @pytest.mark.asyncio
    async def test_custom_blackout_window(self):
        cfg = RiskConfig(resolution_blackout_hours=48.0)
        rm  = RiskManager(config=cfg, bankroll=BANKROLL)
        resolve_ts = time.time() + (36 * 3_600)
        pos = await rm.build_position(
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

    @pytest.mark.asyncio
    async def test_cap_enforced_for_one_trader(self):
        rm = self._rm(0.05)   # cap = 5% * 10k = $500
        await rm.build_position("p1", "mkt_A", "t1", "0xWHALE",
                          entry_price=0.50, size_shares=800.0)   # $400
        with pytest.raises(ExposureCapError, match="Trader"):
            await rm.build_position("p2", "mkt_B", "t2", "0xWHALE",
                              entry_price=0.50, size_shares=400.0)  # +$200 → $600 > $500

    @pytest.mark.asyncio
    async def test_different_traders_independent(self):
        rm = self._rm(0.05)
        await rm.build_position("p1", "mkt_A", "t1", "0xWHALE",
                          entry_price=0.50, size_shares=900.0)   # $450
        # A different trader has its own independent cap.
        await rm.build_position("p2", "mkt_B", "t2", "0xOTHER",
                          entry_price=0.50, size_shares=900.0)   # $450
        assert rm.trader_exposure("0xWHALE") == pytest.approx(450.0)
        assert rm.trader_exposure("0xOTHER") == pytest.approx(450.0)

    @pytest.mark.asyncio
    async def test_exposure_released_on_exit(self):
        rm = self._rm(0.05)
        pos = await rm.build_position("p1", "mkt_A", "t1", "0xWHALE",
                                entry_price=0.50, size_shares=900.0)  # $450
        await rm.record_exit(pos, 0.50)
        assert rm.trader_exposure("0xWHALE") == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_release_exposure_frees_trader_allocation(self):
        rm = self._rm(0.05)
        await rm.build_position("p1", "mkt_A", "t1", "0xWHALE",
                          entry_price=0.50, size_shares=900.0)  # $450
        await rm.release_exposure("mkt_A", 450.0, "0xWHALE")
        assert rm.trader_exposure("0xWHALE") == pytest.approx(0.0)
        # And the trader can be copied again afterwards.
        await rm.build_position("p2", "mkt_A", "t1", "0xWHALE",
                          entry_price=0.50, size_shares=900.0)


# ─── [J] Trading Halt: daily-loss breaker + post-loss cooldown ────────────────

class TestTradingHalt:

    def test_not_halted_by_default(self, rm):
        assert rm.is_trading_halted() is None

    @pytest.mark.asyncio
    async def test_halted_after_daily_loss_limit(self):
        rm = RiskManager(
            config=RiskConfig(
                daily_loss_limit_pct=0.03,
                max_market_exposure_pct=1.0,   # keep market/trader caps out of the
                max_trader_allocation=1.0,     # way so we can drive the loss
            ),
            bankroll=BANKROLL,
        )
        # Drive daily PnL below -3% * 10k = -$300 via a losing exit.
        pos = await rm.build_position("p1", "mkt_A", "t1", "0xA",
                                entry_price=0.50, size_shares=4_000.0)  # $2000 notional
        await rm.record_exit(pos, 0.40)   # -0.10 * 4000 = -$400 < -$300
        reason = rm.is_trading_halted()
        assert reason is not None
        assert "daily loss" in reason

    @pytest.mark.asyncio
    async def test_cooldown_engages_after_consecutive_losses(self):
        rm = RiskManager(
            config=RiskConfig(
                cooldown_after_losses=3, cooldown_minutes=60,
                daily_loss_limit_pct=1.0,  # keep daily breaker out of the way
            ),
            bankroll=BANKROLL,
        )
        for i in range(3):
            pos = await rm.build_position(f"p{i}", "mkt_A", f"t{i}", "0xA",
                                    entry_price=0.50, size_shares=100.0)
            await rm.record_exit(pos, 0.49)   # small loss
        reason = rm.is_trading_halted()
        assert reason is not None
        assert "cooldown" in reason

    @pytest.mark.asyncio
    async def test_win_resets_loss_streak(self):
        rm = RiskManager(
            config=RiskConfig(
                cooldown_after_losses=3, cooldown_minutes=60, daily_loss_limit_pct=1.0,
            ),
            bankroll=BANKROLL,
        )
        for i in range(2):
            pos = await rm.build_position(f"p{i}", "mkt_A", f"t{i}", "0xA",
                                    entry_price=0.50, size_shares=100.0)
            await rm.record_exit(pos, 0.49)   # two losses
        win = await rm.build_position("pw", "mkt_A", "tw", "0xA",
                                entry_price=0.50, size_shares=100.0)
        await rm.record_exit(win, 0.60)       # a win resets the streak
        loss = await rm.build_position("pl", "mkt_A", "tl", "0xA",
                                 entry_price=0.50, size_shares=100.0)
        await rm.record_exit(loss, 0.49)      # one more loss — streak is 1, not 3
        assert rm.is_trading_halted() is None


# ─── [N] Midnight UTC correctness ────────────────────────────────────────────

class TestMidnightUtc:
    """_midnight_utc() must return 00:00:00 UTC regardless of the host timezone."""

    def test_returns_midnight_utc(self):
        ts = _midnight_utc()
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        assert dt.hour == 0
        assert dt.minute == 0
        assert dt.second == 0
        assert dt.microsecond == 0

    def test_is_today_or_yesterday_utc(self):
        ts = _midnight_utc()
        now_utc = datetime.now(timezone.utc)
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        # The returned midnight must be within 24h before now
        assert 0 <= (now_utc - dt).total_seconds() < 86_400

    def test_window_resets_after_24h(self, rm):
        """Daily window resets after 86400 seconds have elapsed."""
        # Force the start timestamp far in the past to trigger a reset
        rm._day_start_ts = time.time() - 90_000  # 25 hours ago
        rm._daily_pnl = Decimal("-999")
        # Any evaluate/is_trading_halted call triggers _maybe_reset_daily_window
        rm.is_trading_halted()
        assert rm._daily_pnl == Decimal("0")


# ─── [O] Exposure cap restored correctly on restart ──────────────────────────

class TestExposureRestoration:
    """Simulate the startup loop in main.py that reconstructs exposure from DB."""

    @pytest.mark.asyncio
    async def test_restored_exposure_is_enforced(self):
        rm = RiskManager(config=RiskConfig(max_trader_allocation=1.0), bankroll=BANKROLL)
        # Simulate main.py restoring an existing position
        existing_value = Decimal("700")  # $700 already in mkt_A
        rm._market_exposure["mkt_A"] = existing_value

        # Cap = 8% of $10,000 = $800.  $700 already in, only $100 headroom.
        # A new position worth $200 should breach the cap.
        with pytest.raises(ExposureCapError):
            await rm.build_position(
                "new_pos", "mkt_A", "tok_A", "0xNEW",
                entry_price=0.50, size_shares=400.0,  # $200 at 0.50
            )

    @pytest.mark.asyncio
    async def test_restored_exposure_allows_under_cap(self):
        rm = RiskManager(config=RiskConfig(max_trader_allocation=1.0), bankroll=BANKROLL)
        rm._market_exposure["mkt_A"] = Decimal("700")  # $700 already

        # $50 new position fits under the $800 cap
        pos = await rm.build_position(
            "new_pos", "mkt_A", "tok_A", "0xNEW",
            entry_price=0.50, size_shares=100.0,  # $50 at 0.50
        )
        assert pos is not None
        assert float(rm._market_exposure["mkt_A"]) == pytest.approx(750.0)

    def test_no_double_counting_on_same_market(self):
        """Adding exposure for the same market accumulates, not overwrites."""
        rm = RiskManager(config=RiskConfig(max_trader_allocation=1.0), bankroll=BANKROLL)
        # Simulates restoring two open positions in the same market
        rm._market_exposure["mkt_A"] = (
            rm._market_exposure.get("mkt_A", Decimal("0")) + Decimal("300")
        )
        rm._market_exposure["mkt_A"] = (
            rm._market_exposure.get("mkt_A", Decimal("0")) + Decimal("300")
        )
        assert float(rm._market_exposure["mkt_A"]) == pytest.approx(600.0)


class TestRehydrateExposure:
    """Restoring exposure for already-open positions on restart."""

    def test_registers_market_and_trader_exposure(self):
        rm = RiskManager(config=RiskConfig(), bankroll=10_000.0)
        rm.rehydrate_exposure(market_id="mkt-a", trader_address="0xA", value=250.0)
        assert rm.market_exposure("mkt-a") == pytest.approx(250.0)
        assert rm.trader_exposure("0xA") == pytest.approx(250.0)

    def test_accumulates_across_positions(self):
        rm = RiskManager(config=RiskConfig(), bankroll=10_000.0)
        rm.rehydrate_exposure("mkt-a", "0xA", 100.0)
        rm.rehydrate_exposure("mkt-a", "0xA", 50.0)
        assert rm.market_exposure("mkt-a") == pytest.approx(150.0)
        assert rm.trader_exposure("0xA") == pytest.approx(150.0)

    def test_over_cap_is_tracked_not_rejected(self, caplog):
        # max_market_exposure_pct default 0.08 -> $800 cap on $10k bankroll.
        rm = RiskManager(config=RiskConfig(), bankroll=10_000.0)
        with caplog.at_level("WARNING"):
            # Restoring a $1,000 position must NOT raise (the position exists),
            # but must warn that it breaches the current cap.
            rm.rehydrate_exposure("mkt-a", "0xA", 1_000.0)
        assert rm.market_exposure("mkt-a") == pytest.approx(1_000.0)
        assert any("exceeds current cap" in r.message for r in caplog.records)

    async def test_rehydrated_exposure_feeds_cap_enforcement(self):
        # After restoring near-cap exposure, a new copy that would breach the
        # market cap must be rejected by build_position().
        rm = RiskManager(config=RiskConfig(max_trader_allocation=1.0), bankroll=10_000.0)
        rm.rehydrate_exposure("mkt-a", "0xA", 790.0)  # cap is $800
        with pytest.raises(ExposureCapError):
            await rm.build_position(
                position_id="p1", market_id="mkt-a", token_id="tok-a",
                trader_address="0xB", entry_price=0.50, size_shares=100.0,  # +$50
            )

"""Tests for risk management — the most critical module."""

from __future__ import annotations

import time

import pytest

from polymarket_copier.core.risk_manager import RiskManager
from polymarket_copier.models.types import ExitReason, Position, Trade, TradeSide


class TestTakeProfit:
    @pytest.fixture
    def rm(self, risk_config, copy_config):
        return RiskManager(risk_config, copy_config, bankroll=10000)

    def test_take_profit_triggers_at_15_pct(self, rm):
        pos = Position(
            id="p1", market_id="m1", asset_id="a1",
            side=TradeSide.BUY, size=100,
            entry_price=0.50, current_price=0.58, peak_price=0.58,
        )
        # PnL = (0.58 - 0.50) / 0.50 = 16% >= 15%
        result = rm.check_position_exit(pos)
        assert result == ExitReason.TAKE_PROFIT

    def test_take_profit_no_trigger_at_14_pct(self, rm):
        pos = Position(
            id="p1", market_id="m1", asset_id="a1",
            side=TradeSide.BUY, size=100,
            entry_price=0.50, current_price=0.57, peak_price=0.57,
        )
        # PnL = (0.57 - 0.50) / 0.50 = 14% < 15%
        result = rm.check_position_exit(pos)
        assert result is None

    def test_take_profit_above_threshold(self, rm):
        pos = Position(
            id="p1", market_id="m1", asset_id="a1",
            side=TradeSide.BUY, size=100,
            entry_price=1.00, current_price=1.16, peak_price=1.16,
        )
        # PnL = 16% >= 15%
        result = rm.check_position_exit(pos)
        assert result == ExitReason.TAKE_PROFIT


class TestStopLoss:
    @pytest.fixture
    def rm(self, risk_config, copy_config):
        return RiskManager(risk_config, copy_config, bankroll=10000)

    def test_stop_loss_triggers_at_5_pct(self, rm):
        pos = Position(
            id="p1", market_id="m1", asset_id="a1",
            side=TradeSide.BUY, size=100,
            entry_price=0.50, current_price=0.475, peak_price=0.50,
        )
        result = rm.check_position_exit(pos)
        assert result == ExitReason.STOP_LOSS

    def test_stop_loss_no_trigger_at_4_pct(self, rm):
        pos = Position(
            id="p1", market_id="m1", asset_id="a1",
            side=TradeSide.BUY, size=100,
            entry_price=0.50, current_price=0.48, peak_price=0.50,
        )
        result = rm.check_position_exit(pos)
        assert result is None

    def test_stop_loss_sell_position(self, rm):
        pos = Position(
            id="p1", market_id="m1", asset_id="a1",
            side=TradeSide.SELL, size=100,
            entry_price=0.50, current_price=0.525, peak_price=0.50,
        )
        # SELL position loses when price goes UP
        result = rm.check_position_exit(pos)
        assert result == ExitReason.STOP_LOSS


class TestTrailingStop:
    @pytest.fixture
    def rm(self, risk_config, copy_config):
        return RiskManager(risk_config, copy_config, bankroll=10000)

    def test_trailing_stop_triggers(self, rm):
        # Entry near peak so unrealized PnL is between -5% and +15%
        pos = Position(
            id="p1", market_id="m1", asset_id="a1",
            side=TradeSide.BUY, size=100,
            entry_price=0.65, current_price=0.625, peak_price=0.70,
        )
        # Unrealized PnL = (0.625 - 0.65) / 0.65 = -3.8% (no stop loss, no take profit)
        # Drop from peak = (0.70 - 0.625) / 0.70 = 10.7% >= 10%
        result = rm.check_position_exit(pos)
        assert result == ExitReason.TRAILING_STOP

    def test_trailing_stop_no_trigger(self, rm):
        pos = Position(
            id="p1", market_id="m1", asset_id="a1",
            side=TradeSide.BUY, size=100,
            entry_price=0.68, current_price=0.665, peak_price=0.70,
        )
        # Unrealized PnL = (0.665 - 0.68) / 0.68 = -2.2% (no take profit, no stop loss)
        # Drop from peak = (0.70 - 0.665) / 0.70 = 5% (below 10%)
        result = rm.check_position_exit(pos)
        assert result is None

    def test_trailing_stop_tracks_peak(self, rm):
        pos = Position(
            id="p1", market_id="m1", asset_id="a1",
            side=TradeSide.BUY, size=100,
            entry_price=0.65, current_price=0.65, peak_price=0.65,
        )
        # Price goes up, peak follows
        pos.update_price(0.68)
        assert pos.peak_price == 0.68

        pos.update_price(0.70)
        assert pos.peak_price == 0.70

        # Price drops but peak stays
        pos.update_price(0.68)
        assert pos.peak_price == 0.70

        # Drop = (0.70 - 0.68) / 0.70 = 2.8% < 10%, no trigger
        # PnL = (0.68 - 0.65) / 0.65 = 4.6% < 15%, no take profit
        result = rm.check_position_exit(pos)
        assert result is None

        # Drop further to trigger trailing stop
        pos.update_price(0.625)
        # Drop = (0.70 - 0.625) / 0.70 = 10.7% >= 10%, triggers trailing stop
        # PnL = (0.625 - 0.65) / 0.65 = -3.8%, no take profit, no stop loss
        result = rm.check_position_exit(pos)
        assert result == ExitReason.TRAILING_STOP


class TestTimeExit:
    @pytest.fixture
    def rm(self, risk_config, copy_config):
        return RiskManager(risk_config, copy_config, bankroll=10000)

    def test_time_exit_after_48h_flat(self, rm):
        pos = Position(
            id="p1", market_id="m1", asset_id="a1",
            side=TradeSide.BUY, size=100,
            entry_price=0.50, current_price=0.51, peak_price=0.51,
            entry_time=time.time() - 49 * 3600,  # 49 hours ago
        )
        # PnL = 2% < 3% min_move -> should exit
        result = rm.check_position_exit(pos)
        assert result == ExitReason.TIME_EXIT

    def test_time_exit_skipped_if_moving(self, rm):
        pos = Position(
            id="p1", market_id="m1", asset_id="a1",
            side=TradeSide.BUY, size=100,
            entry_price=0.50, current_price=0.52, peak_price=0.52,
            entry_time=time.time() - 49 * 3600,  # 49 hours ago
        )
        # PnL = 4% > 3% min_move -> should NOT exit
        result = rm.check_position_exit(pos)
        assert result is None  # Take profit not hit either (4% < 15%)

    def test_time_exit_not_triggered_before_48h(self, rm):
        pos = Position(
            id="p1", market_id="m1", asset_id="a1",
            side=TradeSide.BUY, size=100,
            entry_price=0.50, current_price=0.505, peak_price=0.505,
            entry_time=time.time() - 47 * 3600,  # 47 hours ago
        )
        result = rm.check_position_exit(pos)
        assert result is None


class TestCanOpenPosition:
    @pytest.fixture
    def rm(self, risk_config, copy_config):
        return RiskManager(risk_config, copy_config, bankroll=10000)

    @pytest.fixture
    def trade(self):
        return Trade(
            id="t1", trader_address="0xaaa111", market_id="m1", asset_id="a1",
            side=TradeSide.BUY, size=100, price=0.65,
        )

    def test_can_open_when_clear(self, rm, trade):
        allowed, reason = rm.can_open_position(trade, [])
        assert allowed is True
        assert reason == ""

    def test_daily_loss_limit_halts_trading(self, rm, trade):
        rm.daily_pnl = -301  # 3% of 10000 = 300
        allowed, reason = rm.can_open_position(trade, [])
        assert allowed is False
        assert "Daily loss limit" in reason

    def test_cooldown_after_consecutive_losses(self, rm, trade):
        rm.cooldown_until = time.time() + 3600  # 1 hour from now
        allowed, reason = rm.can_open_position(trade, [])
        assert allowed is False
        assert "cooldown" in reason.lower()

    def test_max_concurrent_positions(self, rm, trade):
        positions = [
            Position(
                id=f"p{i}", market_id=f"m{i}", asset_id=f"a{i}",
                side=TradeSide.BUY, size=10, entry_price=0.5, current_price=0.5, peak_price=0.5,
            )
            for i in range(10)
        ]
        allowed, reason = rm.can_open_position(trade, positions)
        assert allowed is False
        assert "Max positions" in reason

    def test_per_trader_drawdown_stop(self, rm, trade):
        rm.trader_pnl["0xaaa111"] = -801  # 8% of 10000 = 800
        allowed, reason = rm.can_open_position(trade, [])
        assert allowed is False
        assert "drawdown stop" in reason

    def test_per_trader_allocation_cap(self, rm, trade):
        # Create positions from the same trader totalling > 5% of bankroll (500)
        positions = [
            Position(
                id="p1", market_id="m1", asset_id="a1",
                side=TradeSide.BUY, size=600, entry_price=1.0, current_price=1.0,
                peak_price=1.0, source_trader="0xaaa111",
            ),
        ]
        allowed, reason = rm.can_open_position(trade, positions)
        assert allowed is False
        assert "allocation cap" in reason


class TestCopySize:
    @pytest.fixture
    def rm(self, risk_config, copy_config):
        return RiskManager(risk_config, copy_config, bankroll=10000)

    def test_copy_size_half_of_original(self, rm):
        size = rm.calculate_copy_size(100)
        assert size == 50  # 0.5x

    def test_copy_size_capped_at_2_pct_bankroll(self, rm):
        # 0.5x of 1000 = 500, but 2% of 10000 = 200
        size = rm.calculate_copy_size(1000)
        assert size == 200

    def test_copy_size_small_trade(self, rm):
        size = rm.calculate_copy_size(10)
        assert size == 5  # 0.5x, well below 2% cap


class TestPriceDeviation:
    @pytest.fixture
    def rm(self, risk_config, copy_config):
        return RiskManager(risk_config, copy_config, bankroll=10000)

    def test_within_deviation(self, rm):
        assert rm.check_price_deviation(0.65, 0.66) is True  # 1.5%

    def test_exceeds_deviation(self, rm):
        assert rm.check_price_deviation(0.65, 0.70) is False  # 7.7%

    def test_zero_price(self, rm):
        assert rm.check_price_deviation(0, 0.65) is False


class TestRecordTradeResult:
    @pytest.fixture
    def rm(self, risk_config, copy_config):
        return RiskManager(risk_config, copy_config, bankroll=10000)

    def test_loss_increments_consecutive(self, rm):
        rm.record_trade_result(-50, "0xaaa")
        assert rm.consecutive_losses == 1
        rm.record_trade_result(-30, "0xaaa")
        assert rm.consecutive_losses == 2

    def test_win_resets_consecutive(self, rm):
        rm.record_trade_result(-50, "0xaaa")
        rm.record_trade_result(-30, "0xaaa")
        rm.record_trade_result(100, "0xaaa")
        assert rm.consecutive_losses == 0

    def test_cooldown_activated_after_3_losses(self, rm):
        rm.record_trade_result(-10, "0xaaa")
        rm.record_trade_result(-10, "0xaaa")
        rm.record_trade_result(-10, "0xaaa")
        assert rm.cooldown_until > time.time()

    def test_daily_pnl_tracked(self, rm):
        rm.record_trade_result(100, "0xaaa")
        rm.record_trade_result(-50, "0xaaa")
        assert rm.daily_pnl == 50

    def test_trader_pnl_tracked(self, rm):
        rm.record_trade_result(100, "0xaaa")
        rm.record_trade_result(-30, "0xaaa")
        assert rm.trader_pnl["0xaaa"] == 70

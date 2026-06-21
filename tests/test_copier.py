"""Tests for the v2 copy-trade engine (paper-mode orchestration)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from polymarket_copier.api.clob_client import ClobClient
from polymarket_copier.config import AppConfig
from polymarket_copier.core.copier import CopyTrader
from polymarket_copier.core.monitor import TradeEvent, TradeType
from polymarket_copier.core.portfolio import PortfolioManager
from polymarket_copier.core.risk_manager import RiskConfig, RiskManager
from polymarket_copier.models.types import Market


@pytest.fixture
def config() -> AppConfig:
    return AppConfig(mode="paper", bankroll=10_000)


@pytest.fixture
async def portfolio(tmp_path):
    pm = PortfolioManager(db_path=str(tmp_path / "copier_test.db"))
    await pm.init()
    yield pm
    await pm.close()


@pytest.fixture
def gamma():
    g = AsyncMock()
    g.get_market = AsyncMock(return_value=Market(
        condition_id="mkt-a", question="Q?", volume_24h=50_000, active=True,
        resolve_time=None,
    ))
    g.get_market_price = AsyncMock(return_value=0.50)
    return g


@pytest.fixture
def copier(config, portfolio, gamma):
    risk = RiskManager(config=RiskConfig(), bankroll=config.bankroll)
    clob = ClobClient(config)
    return CopyTrader(risk, portfolio, clob, gamma, config)


def buy_event(price=0.50, size=100.0, market="mkt-a", token="tok-a", wallet="0xwhale") -> TradeEvent:
    return TradeEvent(
        event_id="e1", wallet_address=wallet, market_id=market, token_id=token,
        outcome_label="Yes", trade_type=TradeType.BUY, price=price,
        size_usdc=size, timestamp=time.time(), transaction_hash="0xhash",
    )


class TestHandleTradeEvent:
    @pytest.mark.asyncio
    async def test_buy_opens_position(self, copier):
        await copier.handle_trade_event(buy_event())
        assert await copier.portfolio.position_count() == 1

    @pytest.mark.asyncio
    async def test_copy_size_is_conservative(self, copier):
        # size_multiplier 0.5 → 50 USDC, well under 2% bankroll cap ($200)
        await copier.handle_trade_event(buy_event(price=0.50, size=100.0))
        positions = await copier.portfolio.get_open_positions()
        assert len(positions) == 1
        # 50 USDC / 0.50 price = 100 shares
        assert positions[0].size_shares == pytest.approx(100.0)

    @pytest.mark.asyncio
    async def test_copy_size_capped_at_bankroll_pct(self, copier):
        # Large source trade → capped at 2% of $10k = $200 → 400 shares @ 0.50
        await copier.handle_trade_event(buy_event(price=0.50, size=100_000.0))
        positions = await copier.portfolio.get_open_positions()
        assert positions[0].size_shares == pytest.approx(400.0)

    @pytest.mark.asyncio
    async def test_sell_event_skipped(self, copier):
        event = buy_event()
        sell = TradeEvent(**{**event.__dict__, "trade_type": TradeType.SELL})
        await copier.handle_trade_event(sell)
        assert await copier.portfolio.position_count() == 0

    @pytest.mark.asyncio
    async def test_price_deviation_skip(self, copier, gamma):
        # Current price 0.60 vs event 0.50 → 20% deviation > 2% max
        gamma.get_market_price = AsyncMock(return_value=0.60)
        await copier.handle_trade_event(buy_event(price=0.50))
        assert await copier.portfolio.position_count() == 0

    @pytest.mark.asyncio
    async def test_low_volume_skip(self, copier, gamma):
        gamma.get_market = AsyncMock(return_value=Market(
            condition_id="mkt-a", volume_24h=100, active=True, resolve_time=None,
        ))
        await copier.handle_trade_event(buy_event())
        assert await copier.portfolio.position_count() == 0

    @pytest.mark.asyncio
    async def test_resolution_blackout_skip(self, copier, gamma):
        from datetime import datetime, timezone, timedelta
        soon = datetime.now(timezone.utc) + timedelta(hours=6)
        gamma.get_market = AsyncMock(return_value=Market(
            condition_id="mkt-a", volume_24h=50_000, active=True, resolve_time=soon,
        ))
        await copier.handle_trade_event(buy_event())
        assert await copier.portfolio.position_count() == 0

    @pytest.mark.asyncio
    async def test_max_concurrent_positions(self, copier):
        copier.config.copy_trading.max_concurrent_positions = 1
        await copier.handle_trade_event(buy_event(market="mkt-a", token="tok-a"))
        await copier.handle_trade_event(buy_event(market="mkt-b", token="tok-b"))
        assert await copier.portfolio.position_count() == 1


class TestOrderFailureExposureRelease:
    """When a copy order fails after exposure is reserved, that reservation must
    be released — otherwise a never-opened position permanently consumes the
    per-market exposure cap and silently blocks future copies in that market."""

    @pytest.mark.asyncio
    async def test_generic_order_failure_releases_exposure(self, copier):
        copier.clob.place_order = AsyncMock(side_effect=RuntimeError("exchange down"))
        await copier.handle_trade_event(buy_event(market="mkt-x", token="tok-x"))
        assert await copier.portfolio.position_count() == 0
        assert copier.risk.market_exposure("mkt-x") == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_insufficient_liquidity_releases_exposure(self, copier):
        from polymarket_copier.api.clob_client import InsufficientLiquidityError
        copier.clob.place_order = AsyncMock(
            side_effect=InsufficientLiquidityError("thin book")
        )
        await copier.handle_trade_event(buy_event(market="mkt-y", token="tok-y"))
        assert await copier.portfolio.position_count() == 0
        assert copier.risk.market_exposure("mkt-y") == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_market_reusable_after_failed_order(self, copier):
        # A failed order must not poison the market: a subsequent good order in
        # the same market should still open (exposure was fully released).
        copier.clob.place_order = AsyncMock(side_effect=RuntimeError("boom"))
        await copier.handle_trade_event(buy_event(market="mkt-z", token="tok-z"))
        assert await copier.portfolio.position_count() == 0

        copier.clob.place_order = AsyncMock(return_value={"status": "PAPER"})
        await copier.handle_trade_event(buy_event(market="mkt-z", token="tok-z"))
        assert await copier.portfolio.position_count() == 1


class TestHandlePriceTick:
    @pytest.mark.asyncio
    async def test_take_profit_exit(self, copier):
        from polymarket_copier.core.monitor import PriceTick
        await copier.handle_trade_event(buy_event(price=0.50, token="tok-a"))
        assert await copier.portfolio.position_count() == 1
        # Price jumps to TP (0.70 for entry 0.50) → position closes
        await copier.handle_price_tick(PriceTick(token_id="tok-a", price=0.72))
        assert await copier.portfolio.position_count() == 0

    @pytest.mark.asyncio
    async def test_hold_no_exit(self, copier):
        from polymarket_copier.core.monitor import PriceTick
        await copier.handle_trade_event(buy_event(price=0.50, token="tok-a"))
        await copier.handle_price_tick(PriceTick(token_id="tok-a", price=0.55))
        assert await copier.portfolio.position_count() == 1

    @pytest.mark.asyncio
    async def test_unknown_token_ignored(self, copier):
        from polymarket_copier.core.monitor import PriceTick
        # No position for this token → no error
        await copier.handle_price_tick(PriceTick(token_id="ghost", price=0.55))
        assert await copier.portfolio.position_count() == 0


class TestStalenessGate:
    @pytest.mark.asyncio
    async def test_stale_trade_skipped(self, copier):
        copier.config.copy_trading.max_trade_age_seconds = 12
        event = buy_event()
        # 60s old → past the 12s budget, alpha decayed.
        stale = TradeEvent(**{**event.__dict__, "timestamp": time.time() - 60})
        await copier.handle_trade_event(stale)
        assert await copier.portfolio.position_count() == 0

    @pytest.mark.asyncio
    async def test_fresh_trade_passes(self, copier):
        copier.config.copy_trading.max_trade_age_seconds = 12
        await copier.handle_trade_event(buy_event())  # timestamp = now
        assert await copier.portfolio.position_count() == 1

    @pytest.mark.asyncio
    async def test_zero_disables_gate(self, copier):
        copier.config.copy_trading.max_trade_age_seconds = 0
        event = buy_event()
        old = TradeEvent(**{**event.__dict__, "timestamp": time.time() - 10_000})
        await copier.handle_trade_event(old)
        assert await copier.portfolio.position_count() == 1


class TestTradingHaltOnEntry:
    @pytest.mark.asyncio
    async def test_entry_blocked_when_halted(self, copier):
        # Daily-loss breaker can no longer be bypassed by opening a new position.
        from unittest.mock import MagicMock
        copier.risk.is_trading_halted = MagicMock(return_value="daily loss limit")
        await copier.handle_trade_event(buy_event())
        assert await copier.portfolio.position_count() == 0


class TestFailClosed:
    @pytest.mark.asyncio
    async def test_skip_when_market_unavailable(self, copier, gamma):
        gamma.get_market = AsyncMock(return_value=None)
        await copier.handle_trade_event(buy_event())
        assert await copier.portfolio.position_count() == 0

    @pytest.mark.asyncio
    async def test_skip_when_price_unavailable(self, copier, gamma):
        gamma.get_market_price = AsyncMock(return_value=None)
        await copier.handle_trade_event(buy_event())
        assert await copier.portfolio.position_count() == 0

    @pytest.mark.asyncio
    async def test_fail_open_when_disabled(self, copier, gamma):
        copier.config.risk_management.fail_closed_on_missing_data = False
        gamma.get_market_price = AsyncMock(return_value=None)
        # With fail-open, falls back to event price and proceeds.
        await copier.handle_trade_event(buy_event())
        assert await copier.portfolio.position_count() == 1


class TestPerTraderAllocationOnCopy:
    @pytest.mark.asyncio
    async def test_trader_cap_blocks_excess_copies(self, copier):
        # Cap a trader at a tiny allocation; a normal copy should breach it.
        copier.risk.cfg.max_trader_allocation = 0.001   # $10 on $10k bankroll
        # Copy size = min(0.5*100, 0.02*10000)= $50 > $10 cap → blocked.
        await copier.handle_trade_event(buy_event(size=100.0))
        assert await copier.portfolio.position_count() == 0
        # And exposure was released (market not poisoned).
        assert copier.risk.trader_exposure("0xwhale") == pytest.approx(0.0)

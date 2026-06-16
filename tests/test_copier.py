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

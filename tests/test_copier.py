"""Tests for the copy trade engine."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from polymarket_copier.api.clob_client import ClobClient
from polymarket_copier.config import AppConfig
from polymarket_copier.core.copier import CopyTrader
from polymarket_copier.core.portfolio import Portfolio
from polymarket_copier.core.risk_manager import RiskManager
from polymarket_copier.models.types import ExitReason, Position, Trade, TradeSide


class TestCopyTrader:
    @pytest.fixture
    def paper_config(self):
        return AppConfig(mode="paper", bankroll=10000)

    @pytest.fixture
    def clob_client(self, paper_config):
        return ClobClient(paper_config)

    @pytest.fixture
    def copy_trader(self, clob_client, mock_gamma_client, risk_config, copy_config):
        rm = RiskManager(risk_config, copy_config, bankroll=10000)
        portfolio = Portfolio(state_file="/tmp/test_portfolio.json")
        return CopyTrader(clob_client, mock_gamma_client, rm, portfolio)

    @pytest.mark.asyncio
    async def test_handle_buy_trade(self, copy_trader, mock_gamma_client):
        mock_gamma_client.get_market_price.return_value = 0.65
        trade = Trade(
            id="t1", trader_address="0xaaa111", market_id="m1", asset_id="a1",
            side=TradeSide.BUY, size=100, price=0.65,
        )
        await copy_trader.handle_trade(trade)
        assert len(copy_trader.portfolio.positions) == 1
        pos = list(copy_trader.portfolio.positions.values())[0]
        assert pos.size == 50  # 0.5x of 100
        assert pos.entry_price == 0.65

    @pytest.mark.asyncio
    async def test_skip_sell_trade(self, copy_trader):
        trade = Trade(
            id="t1", trader_address="0xaaa111", market_id="m1", asset_id="a1",
            side=TradeSide.SELL, size=100, price=0.65,
        )
        await copy_trader.handle_trade(trade)
        assert len(copy_trader.portfolio.positions) == 0

    @pytest.mark.asyncio
    async def test_skip_stale_trade(self, copy_trader, mock_gamma_client):
        # Price moved 5% from trade price (exceeds 2% max deviation)
        mock_gamma_client.get_market_price.return_value = 0.70
        trade = Trade(
            id="t1", trader_address="0xaaa111", market_id="m1", asset_id="a1",
            side=TradeSide.BUY, size=100, price=0.65,
        )
        await copy_trader.handle_trade(trade)
        assert len(copy_trader.portfolio.positions) == 0

    @pytest.mark.asyncio
    async def test_copy_size_capped(self, copy_trader, mock_gamma_client):
        mock_gamma_client.get_market_price.return_value = 0.65
        trade = Trade(
            id="t1", trader_address="0xaaa111", market_id="m1", asset_id="a1",
            side=TradeSide.BUY, size=1000, price=0.65,
        )
        await copy_trader.handle_trade(trade)
        pos = list(copy_trader.portfolio.positions.values())[0]
        # 0.5x of 1000 = 500, but 2% of 10000 = 200
        assert pos.size == 200

    @pytest.mark.asyncio
    async def test_paper_mode_no_live_api_call(self, copy_trader, mock_gamma_client):
        mock_gamma_client.get_market_price.return_value = 0.65
        trade = Trade(
            id="t1", trader_address="0xaaa111", market_id="m1", asset_id="a1",
            side=TradeSide.BUY, size=100, price=0.65,
        )
        # This should not raise even though no real CLOB connection
        await copy_trader.handle_trade(trade)
        assert len(copy_trader.portfolio.positions) == 1

    @pytest.mark.asyncio
    async def test_blocked_by_risk_manager(self, copy_trader, mock_gamma_client):
        mock_gamma_client.get_market_price.return_value = 0.65
        # Hit daily loss limit
        copy_trader.risk.daily_pnl = -301
        trade = Trade(
            id="t1", trader_address="0xaaa111", market_id="m1", asset_id="a1",
            side=TradeSide.BUY, size=100, price=0.65,
        )
        await copy_trader.handle_trade(trade)
        assert len(copy_trader.portfolio.positions) == 0

    @pytest.mark.asyncio
    async def test_check_exits_triggers_take_profit(self, copy_trader, mock_gamma_client):
        # Add a position that's up 20%
        pos = Position(
            id="pos1", market_id="m1", asset_id="a1",
            side=TradeSide.BUY, size=50,
            entry_price=0.50, current_price=0.58, peak_price=0.58,
            source_trader="0xaaa111",
        )
        copy_trader.portfolio.add_position(pos)

        # Price still above take profit
        mock_gamma_client.get_market_price.return_value = 0.58

        await copy_trader.check_exits()
        # Position should be closed
        assert len(copy_trader.portfolio.positions) == 0

    @pytest.mark.asyncio
    async def test_check_exits_no_trigger(self, copy_trader, mock_gamma_client):
        pos = Position(
            id="pos1", market_id="m1", asset_id="a1",
            side=TradeSide.BUY, size=50,
            entry_price=0.50, current_price=0.52, peak_price=0.52,
            source_trader="0xaaa111",
        )
        copy_trader.portfolio.add_position(pos)

        mock_gamma_client.get_market_price.return_value = 0.52

        await copy_trader.check_exits()
        # Position should still be open (4% gain, below 15% TP)
        assert len(copy_trader.portfolio.positions) == 1

    @pytest.mark.asyncio
    async def test_per_trader_allocation_cap(self, copy_trader, mock_gamma_client):
        mock_gamma_client.get_market_price.return_value = 0.65

        # Fill up trader allocation (5% of 10000 = 500)
        pos = Position(
            id="pos1", market_id="m1", asset_id="a1",
            side=TradeSide.BUY, size=500, entry_price=1.0, current_price=1.0,
            peak_price=1.0, source_trader="0xaaa111",
        )
        copy_trader.portfolio.add_position(pos)

        trade = Trade(
            id="t2", trader_address="0xaaa111", market_id="m2", asset_id="a2",
            side=TradeSide.BUY, size=100, price=0.65,
        )
        await copy_trader.handle_trade(trade)
        # Should be blocked by allocation cap
        assert len(copy_trader.portfolio.positions) == 1  # Only the original

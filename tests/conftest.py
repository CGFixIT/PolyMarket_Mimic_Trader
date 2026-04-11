"""Shared test fixtures for the Polymarket copy trading bot."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from polymarket_copier.config import AppConfig, CopyTradingConfig, RiskManagementConfig, TraderSelectionConfig
from polymarket_copier.models.types import Position, Trade, Trader, TradeSide


@pytest.fixture
def risk_config() -> RiskManagementConfig:
    return RiskManagementConfig(
        take_profit_pct=0.15,
        stop_loss_pct=0.05,
        trailing_stop_pct=0.10,
        time_exit_hours=48,
        time_exit_min_move=0.03,
        daily_loss_limit_pct=0.03,
        drawdown_stop_pct=0.08,
        cooldown_after_losses=3,
        cooldown_minutes=60,
        min_market_volume=5000,
    )


@pytest.fixture
def copy_config() -> CopyTradingConfig:
    return CopyTradingConfig(
        size_multiplier=0.5,
        max_trade_pct=0.02,
        max_trader_allocation=0.05,
        max_price_deviation=0.02,
        max_concurrent_positions=10,
    )


@pytest.fixture
def trader_selection_config() -> TraderSelectionConfig:
    return TraderSelectionConfig(
        min_pnl=10000,
        min_win_rate=0.60,
        min_trades=50,
        rebalance_days=7,
    )


@pytest.fixture
def app_config() -> AppConfig:
    return AppConfig(
        mode="paper",
        polling_interval_seconds=20,
        max_tracked_traders=5,
        bankroll=10000,
    )


@pytest.fixture
def sample_traders() -> list[Trader]:
    return [
        Trader(address="0xaaa111", pnl=50000, win_rate=0.72, total_trades=200, volume=500000),
        Trader(address="0xbbb222", pnl=30000, win_rate=0.65, total_trades=150, volume=300000),
        Trader(address="0xccc333", pnl=5000, win_rate=0.55, total_trades=30, volume=50000),
        Trader(address="0xddd444", pnl=80000, win_rate=0.80, total_trades=400, volume=900000),
        Trader(address="0xeee555", pnl=15000, win_rate=0.62, total_trades=100, volume=150000),
        Trader(address="0xfff666", pnl=-5000, win_rate=0.40, total_trades=200, volume=100000),
    ]


@pytest.fixture
def sample_trade() -> Trade:
    return Trade(
        id="trade-001",
        trader_address="0xaaa111",
        market_id="market-abc",
        asset_id="asset-xyz",
        side=TradeSide.BUY,
        size=100.0,
        price=0.65,
        timestamp=time.time(),
        market_slug="will-x-happen",
        outcome="Yes",
    )


@pytest.fixture
def sample_position() -> Position:
    return Position(
        id="pos-001",
        market_id="market-abc",
        asset_id="asset-xyz",
        side=TradeSide.BUY,
        size=50.0,
        entry_price=0.65,
        current_price=0.65,
        peak_price=0.65,
        entry_time=time.time(),
        source_trader="0xaaa111",
        market_slug="will-x-happen",
        outcome="Yes",
    )


@pytest.fixture
def mock_data_client() -> AsyncMock:
    client = AsyncMock()
    client.get_leaderboard = AsyncMock(return_value=[])
    client.get_trades = AsyncMock(return_value=[])
    client.get_activity = AsyncMock(return_value=[])
    client.close = AsyncMock()
    return client


@pytest.fixture
def mock_gamma_client() -> AsyncMock:
    client = AsyncMock()
    client.get_markets = AsyncMock(return_value=[])
    client.get_market = AsyncMock(return_value={})
    client.get_market_price = AsyncMock(return_value=0.65)
    client.close = AsyncMock()
    return client


@pytest.fixture
def sample_leaderboard_data() -> list[dict]:
    return [
        {
            "address": "0xaaa111",
            "pnl": 50000,
            "volume": 500000,
            "numTrades": 200,
            "wins": 144,
            "losses": 56,
        },
        {
            "address": "0xbbb222",
            "pnl": 30000,
            "volume": 300000,
            "numTrades": 150,
            "wins": 97,
            "losses": 53,
        },
        {
            "address": "0xccc333",
            "pnl": 5000,
            "volume": 50000,
            "numTrades": 30,
            "wins": 16,
            "losses": 14,
        },
        {
            "address": "0xddd444",
            "pnl": 80000,
            "volume": 900000,
            "numTrades": 400,
            "wins": 320,
            "losses": 80,
        },
        {
            "address": "0xeee555",
            "pnl": 15000,
            "volume": 150000,
            "numTrades": 100,
            "wins": 62,
            "losses": 38,
        },
    ]


@pytest.fixture
def sample_raw_trades() -> list[dict]:
    return [
        {
            "id": "t1",
            "side": "BUY",
            "market": "market-abc",
            "asset_id": "asset-xyz",
            "size": 100,
            "price": 0.65,
            "timestamp": time.time(),
            "slug": "will-x-happen",
            "outcome": "Yes",
        },
        {
            "id": "t2",
            "side": "SELL",
            "market": "market-abc",
            "asset_id": "asset-xyz",
            "size": 50,
            "price": 0.75,
            "timestamp": time.time(),
            "slug": "will-x-happen",
            "outcome": "Yes",
        },
        {
            "id": "t3",
            "side": "BUY",
            "market": "market-def",
            "asset_id": "asset-uvw",
            "size": 200,
            "price": 0.40,
            "timestamp": time.time(),
            "slug": "another-market",
            "outcome": "No",
        },
    ]

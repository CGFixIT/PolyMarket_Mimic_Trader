"""Tests for top trader discovery and ranking."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from polymarket_copier.core.tracker import TraderTracker
from polymarket_copier.models.types import Trader


class TestTraderTracker:
    @pytest.fixture
    def tracker(self, mock_data_client, trader_selection_config):
        return TraderTracker(mock_data_client, trader_selection_config, max_traders=3)

    def test_parse_trader(self, tracker):
        raw = {
            "address": "0xabc123",
            "pnl": 50000,
            "volume": 500000,
            "numTrades": 200,
            "wins": 144,
            "losses": 56,
        }
        trader = tracker.parse_trader(raw)
        assert trader.address == "0xabc123"
        assert trader.pnl == 50000
        assert trader.total_trades == 200
        assert trader.win_rate == pytest.approx(0.72, abs=0.01)

    def test_parse_trader_alternative_fields(self, tracker):
        raw = {
            "userAddress": "0xdef456",
            "profit": 25000,
            "totalVolume": 200000,
            "totalTrades": 100,
            "winRate": 0.68,
        }
        trader = tracker.parse_trader(raw)
        assert trader.address == "0xdef456"
        assert trader.pnl == 25000
        assert trader.win_rate == 0.68

    def test_filter_traders_by_min_pnl(self, tracker, sample_traders):
        filtered = tracker.filter_traders(sample_traders)
        # 0xccc333 (pnl=5000 < 10000), 0xfff666 (pnl=-5000) should be excluded
        addresses = [t.address for t in filtered]
        assert "0xccc333" not in addresses
        assert "0xfff666" not in addresses

    def test_filter_traders_by_min_win_rate(self, tracker, sample_traders):
        filtered = tracker.filter_traders(sample_traders)
        # 0xccc333 (win_rate=0.55 < 0.60) should be excluded
        addresses = [t.address for t in filtered]
        assert "0xccc333" not in addresses

    def test_filter_traders_by_min_trades(self, tracker, sample_traders):
        filtered = tracker.filter_traders(sample_traders)
        # 0xccc333 (total_trades=30 < 50) should be excluded
        addresses = [t.address for t in filtered]
        assert "0xccc333" not in addresses

    def test_filter_keeps_qualifying_traders(self, tracker, sample_traders):
        filtered = tracker.filter_traders(sample_traders)
        addresses = [t.address for t in filtered]
        assert "0xaaa111" in addresses
        assert "0xbbb222" in addresses
        assert "0xddd444" in addresses
        assert "0xeee555" in addresses

    def test_rank_traders_returns_top_n(self, tracker, sample_traders):
        filtered = tracker.filter_traders(sample_traders)
        ranked = tracker.rank_traders(filtered)
        assert len(ranked) == 3  # max_traders=3
        # Top trader should have highest score
        assert ranked[0].score >= ranked[1].score >= ranked[2].score

    def test_rank_traders_ddd444_highest(self, tracker, sample_traders):
        filtered = tracker.filter_traders(sample_traders)
        ranked = tracker.rank_traders(filtered)
        # 0xddd444 has highest PnL (80k), highest WR (80%), most trades (400)
        assert ranked[0].address == "0xddd444"

    @pytest.mark.asyncio
    async def test_discover(self, tracker, mock_data_client, sample_leaderboard_data):
        mock_data_client.get_leaderboard.return_value = sample_leaderboard_data
        traders = await tracker.discover()
        assert len(traders) <= 3
        assert len(traders) > 0
        assert all(t.score > 0 for t in traders)

    @pytest.mark.asyncio
    async def test_discover_no_results(self, tracker, mock_data_client):
        mock_data_client.get_leaderboard.return_value = []
        traders = await tracker.discover()
        assert len(traders) == 0

    @pytest.mark.asyncio
    async def test_discover_relaxed_filters(self, tracker, mock_data_client):
        # All traders below threshold but have positive PnL
        mock_data_client.get_leaderboard.return_value = [
            {"address": "0xlow", "pnl": 100, "numTrades": 5, "wins": 3, "losses": 2, "volume": 500},
        ]
        traders = await tracker.discover()
        # Should fall through to relaxed filter (pnl > 0)
        assert len(traders) == 1
        assert traders[0].address == "0xlow"

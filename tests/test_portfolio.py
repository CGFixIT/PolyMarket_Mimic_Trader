"""Tests for portfolio state tracking and persistence."""

from __future__ import annotations

import json
import time

import pytest

from polymarket_copier.core.portfolio import Portfolio
from polymarket_copier.models.types import ExitReason, Position, TradeSide


class TestPortfolio:
    @pytest.fixture
    def portfolio(self, tmp_path):
        return Portfolio(state_file=str(tmp_path / "test_state.json"))

    def test_add_position(self, portfolio, sample_position):
        portfolio.add_position(sample_position)
        assert len(portfolio.positions) == 1
        assert portfolio.total_trades == 1

    def test_close_position_profit(self, portfolio, sample_position):
        sample_position.entry_price = 0.50
        sample_position.current_price = 0.60
        portfolio.add_position(sample_position)

        pnl = portfolio.close_position(sample_position.id, 0.60, ExitReason.TAKE_PROFIT)
        assert pnl is not None
        assert pnl > 0
        assert len(portfolio.positions) == 0
        assert portfolio.winning_trades == 1

    def test_close_position_loss(self, portfolio, sample_position):
        sample_position.entry_price = 0.50
        sample_position.current_price = 0.45
        portfolio.add_position(sample_position)

        pnl = portfolio.close_position(sample_position.id, 0.45, ExitReason.STOP_LOSS)
        assert pnl is not None
        assert pnl < 0
        assert portfolio.winning_trades == 0

    def test_close_nonexistent_position(self, portfolio):
        result = portfolio.close_position("nonexistent", 0.5, ExitReason.MANUAL)
        assert result is None

    def test_unrealized_pnl(self, portfolio):
        pos = Position(
            id="p1", market_id="m1", asset_id="a1",
            side=TradeSide.BUY, size=100,
            entry_price=0.50, current_price=0.55, peak_price=0.55,
        )
        portfolio.add_position(pos)
        assert portfolio.unrealized_pnl == pytest.approx(5.0, abs=0.1)

    def test_total_pnl(self, portfolio):
        pos1 = Position(
            id="p1", market_id="m1", asset_id="a1",
            side=TradeSide.BUY, size=100,
            entry_price=0.50, current_price=0.55, peak_price=0.55,
        )
        portfolio.add_position(pos1)
        portfolio.close_position("p1", 0.55, ExitReason.TAKE_PROFIT)

        pos2 = Position(
            id="p2", market_id="m2", asset_id="a2",
            side=TradeSide.BUY, size=100,
            entry_price=0.50, current_price=0.52, peak_price=0.52,
        )
        portfolio.add_position(pos2)

        # Total = closed (5.0) + unrealized (2.0)
        assert portfolio.total_pnl == pytest.approx(7.0, abs=0.1)

    def test_win_rate(self, portfolio):
        for i in range(3):
            pos = Position(
                id=f"p{i}", market_id="m1", asset_id="a1",
                side=TradeSide.BUY, size=100,
                entry_price=0.50, current_price=0.60, peak_price=0.60,
            )
            portfolio.add_position(pos)
            portfolio.close_position(f"p{i}", 0.60, ExitReason.TAKE_PROFIT)

        pos_loss = Position(
            id="p_loss", market_id="m1", asset_id="a1",
            side=TradeSide.BUY, size=100,
            entry_price=0.50, current_price=0.40, peak_price=0.50,
        )
        portfolio.add_position(pos_loss)
        portfolio.close_position("p_loss", 0.40, ExitReason.STOP_LOSS)

        assert portfolio.win_rate == pytest.approx(0.75, abs=0.01)

    def test_win_rate_no_trades(self, portfolio):
        assert portfolio.win_rate == 0.0

    def test_get_trader_positions(self, portfolio):
        pos1 = Position(
            id="p1", market_id="m1", asset_id="a1",
            side=TradeSide.BUY, size=100, entry_price=0.5,
            current_price=0.5, peak_price=0.5, source_trader="0xaaa",
        )
        pos2 = Position(
            id="p2", market_id="m2", asset_id="a2",
            side=TradeSide.BUY, size=100, entry_price=0.5,
            current_price=0.5, peak_price=0.5, source_trader="0xbbb",
        )
        portfolio.add_position(pos1)
        portfolio.add_position(pos2)

        trader_a = portfolio.get_trader_positions("0xaaa")
        assert len(trader_a) == 1
        assert trader_a[0].id == "p1"

    def test_update_prices(self, portfolio):
        pos = Position(
            id="p1", market_id="m1", asset_id="a1",
            side=TradeSide.BUY, size=100,
            entry_price=0.50, current_price=0.50, peak_price=0.50,
        )
        portfolio.add_position(pos)
        portfolio.update_prices({"a1": 0.60})
        assert portfolio.positions["p1"].current_price == 0.60

    def test_save_and_load(self, portfolio):
        pos = Position(
            id="p1", market_id="m1", asset_id="a1",
            side=TradeSide.BUY, size=100,
            entry_price=0.50, current_price=0.55, peak_price=0.55,
        )
        portfolio.add_position(pos)
        portfolio.closed_pnl = 42.0
        portfolio.save()

        # Load into a new portfolio
        portfolio2 = Portfolio(state_file=portfolio.state_file)
        portfolio2.load()
        assert len(portfolio2.positions) == 1
        assert portfolio2.positions["p1"].entry_price == 0.50
        assert portfolio2.closed_pnl == 42.0

    def test_load_nonexistent_file(self, tmp_path):
        portfolio = Portfolio(state_file=str(tmp_path / "nonexistent.json"))
        portfolio.load()  # Should not raise
        assert len(portfolio.positions) == 0

    def test_summary(self, portfolio, sample_position):
        portfolio.add_position(sample_position)
        summary = portfolio.summary()
        assert "Portfolio Summary" in summary
        assert "Open positions: 1" in summary

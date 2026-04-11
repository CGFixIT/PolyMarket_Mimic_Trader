"""Tests for Pydantic data models."""

from __future__ import annotations

import time

import pytest

from polymarket_copier.models.types import (
    CopyOrder,
    ExitReason,
    Position,
    Trade,
    TradeRecord,
    Trader,
    TradeSide,
)


class TestTrader:
    def test_compute_score(self):
        trader = Trader(address="0xabc", pnl=50000, win_rate=0.72, total_trades=200)
        score = trader.compute_score()
        assert score > 0
        assert score == trader.score

    def test_compute_score_max_pnl(self):
        trader = Trader(address="0xabc", pnl=200000, win_rate=1.0, total_trades=600)
        score = trader.compute_score()
        # pnl capped at 1.0 (200k/100k=2.0 -> min(2.0,1.0)=1.0)
        # win_rate = 1.0, consistency = min(600/500,1.0) = 1.0
        assert score == pytest.approx(1.0, abs=0.01)

    def test_compute_score_zero_pnl(self):
        trader = Trader(address="0xabc", pnl=0, win_rate=0.5, total_trades=100)
        score = trader.compute_score()
        assert score > 0  # win_rate and consistency still contribute

    def test_compute_score_negative_pnl(self):
        trader = Trader(address="0xabc", pnl=-10000, win_rate=0.4, total_trades=50)
        score = trader.compute_score()
        # pnl_norm = 0 (negative), but win_rate and consistency still contribute
        assert score > 0


class TestTrade:
    def test_create_buy_trade(self):
        trade = Trade(
            id="t1",
            trader_address="0xabc",
            market_id="m1",
            asset_id="a1",
            side=TradeSide.BUY,
            size=100,
            price=0.65,
        )
        assert trade.side == TradeSide.BUY
        assert trade.size == 100
        assert trade.price == 0.65

    def test_create_sell_trade(self):
        trade = Trade(
            id="t2",
            trader_address="0xabc",
            market_id="m1",
            asset_id="a1",
            side=TradeSide.SELL,
            size=50,
            price=0.80,
        )
        assert trade.side == TradeSide.SELL

    def test_trade_default_timestamp(self):
        before = time.time()
        trade = Trade(
            id="t1", trader_address="0x", market_id="m", asset_id="a",
            side=TradeSide.BUY, size=1, price=0.5,
        )
        after = time.time()
        assert before <= trade.timestamp <= after


class TestPosition:
    def test_unrealized_pnl_buy_profit(self):
        pos = Position(
            id="p1", market_id="m1", asset_id="a1",
            side=TradeSide.BUY, size=100,
            entry_price=0.50, current_price=0.60, peak_price=0.60,
        )
        assert pos.unrealized_pnl_pct == pytest.approx(0.20, abs=0.01)
        assert pos.unrealized_pnl == pytest.approx(10.0, abs=0.1)

    def test_unrealized_pnl_buy_loss(self):
        pos = Position(
            id="p1", market_id="m1", asset_id="a1",
            side=TradeSide.BUY, size=100,
            entry_price=0.50, current_price=0.45, peak_price=0.50,
        )
        assert pos.unrealized_pnl_pct == pytest.approx(-0.10, abs=0.01)
        assert pos.unrealized_pnl == pytest.approx(-5.0, abs=0.1)

    def test_unrealized_pnl_sell_profit(self):
        pos = Position(
            id="p1", market_id="m1", asset_id="a1",
            side=TradeSide.SELL, size=100,
            entry_price=0.50, current_price=0.40, peak_price=0.40,
        )
        assert pos.unrealized_pnl_pct == pytest.approx(0.20, abs=0.01)

    def test_unrealized_pnl_zero_entry(self):
        pos = Position(
            id="p1", market_id="m1", asset_id="a1",
            side=TradeSide.BUY, size=100,
            entry_price=0, current_price=0.50, peak_price=0.50,
        )
        assert pos.unrealized_pnl_pct == 0.0

    def test_update_price_tracks_peak_buy(self):
        pos = Position(
            id="p1", market_id="m1", asset_id="a1",
            side=TradeSide.BUY, size=100,
            entry_price=0.50, current_price=0.50, peak_price=0.50,
        )
        pos.update_price(0.60)
        assert pos.peak_price == 0.60
        assert pos.current_price == 0.60

        pos.update_price(0.55)
        assert pos.peak_price == 0.60  # Peak stays
        assert pos.current_price == 0.55

    def test_drop_from_peak_buy(self):
        pos = Position(
            id="p1", market_id="m1", asset_id="a1",
            side=TradeSide.BUY, size=100,
            entry_price=0.50, current_price=0.54, peak_price=0.60,
        )
        # Drop = (0.60 - 0.54) / 0.60 = 0.10
        assert pos.drop_from_peak_pct == pytest.approx(0.10, abs=0.01)

    def test_age_hours(self):
        pos = Position(
            id="p1", market_id="m1", asset_id="a1",
            side=TradeSide.BUY, size=100,
            entry_price=0.50, current_price=0.50, peak_price=0.50,
            entry_time=time.time() - 7200,  # 2 hours ago
        )
        assert pos.age_hours == pytest.approx(2.0, abs=0.1)

    def test_serialization(self):
        pos = Position(
            id="p1", market_id="m1", asset_id="a1",
            side=TradeSide.BUY, size=100,
            entry_price=0.50, current_price=0.55, peak_price=0.55,
        )
        data = pos.model_dump()
        restored = Position(**data)
        assert restored.id == pos.id
        assert restored.unrealized_pnl_pct == pos.unrealized_pnl_pct


class TestCopyOrder:
    def test_create_order(self, sample_trade):
        order = CopyOrder(
            market_id="m1",
            asset_id="a1",
            side=TradeSide.BUY,
            size=50,
            price=0.65,
            source_trade=sample_trade,
            source_trader="0xabc",
        )
        assert order.size == 50
        assert order.source_trade.id == "trade-001"


class TestTradeRecord:
    def test_defaults(self, sample_trade):
        order = CopyOrder(
            market_id="m1", asset_id="a1", side=TradeSide.BUY,
            size=50, price=0.65, source_trade=sample_trade,
        )
        record = TradeRecord(order=order)
        assert record.paper is True
        assert record.executed is False
        assert record.pnl is None
        assert record.exit_reason is None

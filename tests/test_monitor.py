"""Tests for the v2 WebSocket-first trade monitor."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from polymarket_copier.core.monitor import (
    PriceTick,
    TradeEvent,
    TradeMonitor,
    TradeType,
    _parse_trade_event,
)


async def _noop_trade(event):
    """Async no-op trade callback (callbacks must be awaitable)."""
    return None


class TestParseTradeEvent:
    def test_parse_buy(self):
        raw = {
            "id": "t1", "side": "BUY", "market": "mkt-a", "asset": "tok-a",
            "price": "0.65", "size": "100", "timestamp": 1_700_000_000,
        }
        event = _parse_trade_event("0xabc", raw)
        assert event is not None
        assert event.trade_type == TradeType.BUY
        assert event.price == 0.65
        assert event.size_usdc == 100
        assert event.wallet_address == "0xabc"

    def test_parse_sell(self):
        raw = {
            "id": "t2", "side": "SELL", "market": "mkt-a", "asset": "tok-a",
            "price": "0.75", "size": "50", "timestamp": 1_700_000_000,
        }
        event = _parse_trade_event("0xabc", raw)
        assert event.trade_type == TradeType.SELL

    def test_missing_market_returns_none(self):
        raw = {"id": "t1", "side": "BUY", "price": "0.5", "size": "10"}
        assert _parse_trade_event("0xabc", raw) is None

    def test_zero_price_returns_none(self):
        raw = {
            "id": "t1", "side": "BUY", "market": "m", "asset": "a",
            "price": "0", "size": "10",
        }
        assert _parse_trade_event("0xabc", raw) is None

    def test_millis_timestamp_normalized(self):
        raw = {
            "id": "t1", "side": "BUY", "market": "m", "asset": "a",
            "price": "0.5", "size": "10", "timestamp": 1_700_000_000_000,
        }
        event = _parse_trade_event("0xabc", raw)
        assert event.timestamp == pytest.approx(1_700_000_000, abs=1)


class TestTradeMonitor:
    def test_requires_wallets(self):
        with pytest.raises(ValueError, match="non-empty"):
            TradeMonitor(tracked_wallets=[], on_trade=lambda e: None)

    def test_lowercases_wallets(self):
        monitor = TradeMonitor(
            tracked_wallets=["0xABCDEF"], on_trade=lambda e: None,
        )
        assert monitor._wallets == ["0xabcdef"]

    def test_subscribe_unsubscribe_token(self):
        monitor = TradeMonitor(tracked_wallets=["0xabc"], on_trade=lambda e: None)
        monitor.subscribe_token("tok-1")
        assert "tok-1" in monitor._subscribed_tokens
        monitor.unsubscribe_token("tok-1")
        assert "tok-1" not in monitor._subscribed_tokens

    def test_filter_new_trades_dedup(self):
        monitor = TradeMonitor(tracked_wallets=["0xabc"], on_trade=lambda e: None)
        activity = [
            {"id": "t1", "type": "trade", "side": "BUY"},
            {"id": "t2", "type": "trade", "side": "SELL"},
        ]
        first = monitor._filter_new_trades("0xabc", activity)
        assert len(first) == 2
        # Second pass: all already seen
        second = monitor._filter_new_trades("0xabc", activity)
        assert len(second) == 0

    def test_filter_ignores_non_trades(self):
        monitor = TradeMonitor(tracked_wallets=["0xabc"], on_trade=lambda e: None)
        activity = [
            {"id": "x1", "type": "transfer"},
            {"id": "t1", "type": "trade", "side": "BUY"},
        ]
        new = monitor._filter_new_trades("0xabc", activity)
        assert len(new) == 1
        assert new[0]["id"] == "t1"

    @pytest.mark.asyncio
    async def test_handle_ws_message_emits_price_tick(self):
        ticks = []

        async def on_price(t):
            ticks.append(t)

        monitor = TradeMonitor(
            tracked_wallets=["0xabc"],
            on_trade=_noop_trade,
            on_price=on_price,
        )
        monitor.subscribe_token("tok-a")
        import json
        raw = json.dumps([
            {"event_type": "price_change", "asset_id": "tok-a", "price": "0.55"}
        ])
        await monitor._handle_ws_message(raw)
        assert len(ticks) == 1
        assert isinstance(ticks[0], PriceTick)
        assert ticks[0].price == 0.55

    @pytest.mark.asyncio
    async def test_handle_ws_message_ignores_unsubscribed(self):
        ticks = []

        async def on_price(t):
            ticks.append(t)

        monitor = TradeMonitor(
            tracked_wallets=["0xabc"],
            on_trade=_noop_trade,
            on_price=on_price,
        )
        import json
        raw = json.dumps([
            {"event_type": "price_change", "asset_id": "other-tok", "price": "0.55"}
        ])
        await monitor._handle_ws_message(raw)
        assert len(ticks) == 0

    @pytest.mark.asyncio
    async def test_handle_ws_message_rejects_out_of_range_price(self):
        ticks = []

        async def on_price(t):
            ticks.append(t)

        monitor = TradeMonitor(
            tracked_wallets=["0xabc"],
            on_trade=_noop_trade,
            on_price=on_price,
        )
        monitor.subscribe_token("tok-a")
        import json
        raw = json.dumps([
            {"event_type": "price_change", "asset_id": "tok-a", "price": "1.5"}
        ])
        await monitor._handle_ws_message(raw)
        assert len(ticks) == 0


class TestColdStartPriming:
    def test_first_poll_primes_without_emitting(self):
        monitor = TradeMonitor(tracked_wallets=["0xabc"], on_trade=_noop_trade)
        activity = [
            {"id": "t1", "type": "trade", "side": "BUY"},
            {"id": "t2", "type": "trade", "side": "SELL"},
        ]
        # prime=True seeds the baseline but returns nothing.
        primed = monitor._filter_new_trades("0xabc", activity, prime=True)
        assert primed == []
        # Both ids are now recorded as seen.
        assert "t1" in monitor._seen_trade_ids["0xabc"]
        assert "t2" in monitor._seen_trade_ids["0xabc"]
        # A subsequent normal poll of the same activity emits nothing (all seen).
        assert monitor._filter_new_trades("0xabc", activity) == []

    def test_new_trade_after_priming_is_emitted(self):
        monitor = TradeMonitor(tracked_wallets=["0xabc"], on_trade=_noop_trade)
        monitor._filter_new_trades("0xabc", [{"id": "t1", "type": "trade", "side": "BUY"}], prime=True)
        new = monitor._filter_new_trades(
            "0xabc",
            [
                {"id": "t1", "type": "trade", "side": "BUY"},
                {"id": "t2", "type": "trade", "side": "BUY"},
            ],
        )
        assert len(new) == 1
        assert new[0]["id"] == "t2"

    def test_prime_on_start_false_acts_immediately(self):
        monitor = TradeMonitor(
            tracked_wallets=["0xabc"], on_trade=_noop_trade, prime_on_start=False,
        )
        assert "0xabc" in monitor._primed_wallets


class TestSeenEvictionFIFO:
    def test_oldest_ids_evicted_first(self):
        from polymarket_copier.core.monitor import _MAX_TRADES_PER_POLL
        monitor = TradeMonitor(tracked_wallets=["0xabc"], on_trade=_noop_trade)
        # Insert well over the 2x cap so eviction runs.
        n = _MAX_TRADES_PER_POLL * 2 + 10
        activity = [
            {"id": f"t{i}", "type": "trade", "side": "BUY"} for i in range(n)
        ]
        monitor._filter_new_trades("0xabc", activity)
        seen = monitor._seen_trade_ids["0xabc"]
        # Size is bounded to the 2x cap.
        assert len(seen) <= _MAX_TRADES_PER_POLL * 2
        # The OLDEST ids were dropped; the most recent ids are retained.
        assert f"t{n - 1}" in seen
        assert "t0" not in seen

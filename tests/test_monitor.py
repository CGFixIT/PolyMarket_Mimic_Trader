"""Tests for the v2 WebSocket-first trade monitor."""

from __future__ import annotations

import time
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


# ─── Latency instrumentation ──────────────────────────────────────────────────

class TestTradeEventDetectedAt:
    """The detected_at field stamps each event with a monotonic clock reading
    at parse time, allowing copier.py to measure detection → order latency."""

    def test_detected_at_defaults_to_monotonic_now(self):
        before = time.monotonic()
        event = TradeEvent(
            event_id="e1", wallet_address="0xw", market_id="m",
            token_id="t", outcome_label="Yes", trade_type=TradeType.BUY,
            price=0.5, size_usdc=100.0, timestamp=time.time(),
            transaction_hash="0xh",
        )
        after = time.monotonic()
        assert before <= event.detected_at <= after

    def test_detected_at_can_be_set_explicitly(self):
        event = TradeEvent(
            event_id="e1", wallet_address="0xw", market_id="m",
            token_id="t", outcome_label="Yes", trade_type=TradeType.BUY,
            price=0.5, size_usdc=100.0, timestamp=time.time(),
            transaction_hash="0xh", detected_at=42.0,
        )
        assert event.detected_at == 42.0

    def test_parse_trade_event_stamps_detected_at(self):
        raw = {
            "id": "t1", "side": "BUY", "market": "mkt-a", "asset": "tok-a",
            "price": "0.5", "size": "100", "timestamp": 1_700_000_000,
        }
        before = time.monotonic()
        event = _parse_trade_event("0xabc", raw)
        after = time.monotonic()
        assert event is not None
        assert before <= event.detected_at <= after


# ─── Rate limiter on hot poll path ────────────────────────────────────────────

class _FakeRespMonitor:
    def __init__(self, data):
        self._data = data
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._data


class _FakeSessionMonitor:
    def __init__(self, data=None):
        self._data = data or []

    def get(self, url, params=None):
        return _FakeRespMonitor(self._data)


class TestRateLimiterOnPollPath:
    def test_default_rate_limiter_is_created(self):
        from aiolimiter import AsyncLimiter
        monitor = TradeMonitor(tracked_wallets=["0xabc"], on_trade=_noop_trade)
        assert isinstance(monitor._rate_limiter, AsyncLimiter)

    def test_injected_rate_limiter_is_stored(self):
        from aiolimiter import AsyncLimiter
        limiter = AsyncLimiter(50, 60)
        monitor = TradeMonitor(
            tracked_wallets=["0xabc"], on_trade=_noop_trade, rate_limiter=limiter,
        )
        assert monitor._rate_limiter is limiter

    @pytest.mark.asyncio
    async def test_rate_limiter_acquired_during_poll(self):
        """The rate limiter's __aenter__ must be called for every poll request."""
        acquired = []

        class _TrackingLimiter:
            async def __aenter__(self):
                acquired.append(True)

            async def __aexit__(self, *a):
                pass

        monitor = TradeMonitor(
            tracked_wallets=["0xWHALE"],
            on_trade=_noop_trade,
            rate_limiter=_TrackingLimiter(),
            prime_on_start=True,
        )
        await monitor._poll_wallet(_FakeSessionMonitor([]), "0xwhale")
        assert len(acquired) == 1, "Rate limiter was not acquired during _poll_wallet"

    @pytest.mark.asyncio
    async def test_detected_at_set_during_real_poll(self):
        """Events emitted by the poll path must have detected_at near time.monotonic()."""
        received = []

        async def capture(event):
            received.append(event)

        monitor = TradeMonitor(
            tracked_wallets=["0xWHALE"],
            on_trade=capture,
            prime_on_start=False,
        )
        data = [{
            "id": "t1", "type": "trade", "side": "BUY",
            "market": "mkt-a", "asset": "tok-a",
            "price": "0.50", "size": "100", "timestamp": 1_700_000_000,
        }]
        before = time.monotonic()
        await monitor._poll_wallet(_FakeSessionMonitor(data), "0xwhale")
        after = time.monotonic()

        assert len(received) == 1
        assert before <= received[0].detected_at <= after

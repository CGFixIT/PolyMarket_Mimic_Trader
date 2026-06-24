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
            "id": "t1",
            "side": "BUY",
            "market": "mkt-a",
            "asset": "tok-a",
            "price": "0.65",
            "size": "100",
            "timestamp": 1_700_000_000,
        }
        event = _parse_trade_event("0xabc", raw)
        assert event is not None
        assert event.trade_type == TradeType.BUY
        assert event.price == 0.65
        assert event.size_usdc == 100
        assert event.wallet_address == "0xabc"

    def test_parse_sell(self):
        raw = {
            "id": "t2",
            "side": "SELL",
            "market": "mkt-a",
            "asset": "tok-a",
            "price": "0.75",
            "size": "50",
            "timestamp": 1_700_000_000,
        }
        event = _parse_trade_event("0xabc", raw)
        assert event.trade_type == TradeType.SELL

    def test_missing_market_returns_none(self):
        raw = {"id": "t1", "side": "BUY", "price": "0.5", "size": "10"}
        assert _parse_trade_event("0xabc", raw) is None

    def test_zero_price_returns_none(self):
        raw = {
            "id": "t1",
            "side": "BUY",
            "market": "m",
            "asset": "a",
            "price": "0",
            "size": "10",
        }
        assert _parse_trade_event("0xabc", raw) is None

    def test_millis_timestamp_normalized(self):
        raw = {
            "id": "t1",
            "side": "BUY",
            "market": "m",
            "asset": "a",
            "price": "0.5",
            "size": "10",
            "timestamp": 1_700_000_000_000,
        }
        event = _parse_trade_event("0xabc", raw)
        assert event.timestamp == pytest.approx(1_700_000_000, abs=1)


class TestTradeMonitor:
    def test_requires_wallets(self):
        with pytest.raises(ValueError, match="non-empty"):
            TradeMonitor(tracked_wallets=[], on_trade=lambda e: None)

    def test_lowercases_wallets(self):
        monitor = TradeMonitor(
            tracked_wallets=["0xABCDEF"],
            on_trade=lambda e: None,
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

        raw = json.dumps([{"event_type": "price_change", "asset_id": "tok-a", "price": "0.55"}])
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

        raw = json.dumps([{"event_type": "price_change", "asset_id": "other-tok", "price": "0.55"}])
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

        raw = json.dumps([{"event_type": "price_change", "asset_id": "tok-a", "price": "1.5"}])
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
            tracked_wallets=["0xabc"],
            on_trade=_noop_trade,
            prime_on_start=False,
        )
        assert "0xabc" in monitor._primed_wallets


class TestSeenEvictionFIFO:
    def test_oldest_ids_evicted_first(self):
        from polymarket_copier.core.monitor import _MAX_TRADES_PER_POLL

        monitor = TradeMonitor(tracked_wallets=["0xabc"], on_trade=_noop_trade)
        # Insert well over the 2x cap so eviction runs.
        n = _MAX_TRADES_PER_POLL * 2 + 10
        activity = [{"id": f"t{i}", "type": "trade", "side": "BUY"} for i in range(n)]
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
            event_id="e1",
            wallet_address="0xw",
            market_id="m",
            token_id="t",
            outcome_label="Yes",
            trade_type=TradeType.BUY,
            price=0.5,
            size_usdc=100.0,
            timestamp=time.time(),
            transaction_hash="0xh",
        )
        after = time.monotonic()
        assert before <= event.detected_at <= after

    def test_detected_at_can_be_set_explicitly(self):
        event = TradeEvent(
            event_id="e1",
            wallet_address="0xw",
            market_id="m",
            token_id="t",
            outcome_label="Yes",
            trade_type=TradeType.BUY,
            price=0.5,
            size_usdc=100.0,
            timestamp=time.time(),
            transaction_hash="0xh",
            detected_at=42.0,
        )
        assert event.detected_at == 42.0

    def test_parse_trade_event_stamps_detected_at(self):
        raw = {
            "id": "t1",
            "side": "BUY",
            "market": "mkt-a",
            "asset": "tok-a",
            "price": "0.5",
            "size": "100",
            "timestamp": 1_700_000_000,
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
            tracked_wallets=["0xabc"],
            on_trade=_noop_trade,
            rate_limiter=limiter,
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
        data = [
            {
                "id": "t1",
                "type": "trade",
                "side": "BUY",
                "market": "mkt-a",
                "asset": "tok-a",
                "price": "0.50",
                "size": "100",
                "timestamp": 1_700_000_000,
            }
        ]
        before = time.monotonic()
        await monitor._poll_wallet(_FakeSessionMonitor(data), "0xwhale")
        after = time.monotonic()

        assert len(received) == 1
        assert before <= received[0].detected_at <= after


# ─── WebSocket Reconnect Fallback ─────────────────────────────────────────────


class TestWsReconnectFallback:
    """H10: _ws_loop NEVER permanently exits — it retries indefinitely with capped backoff."""

    @pytest.mark.asyncio
    async def test_ws_loop_continues_past_max_retries(self):
        # H10: loop must keep retrying past _MAX_WS_RETRIES (old behavior was to exit).
        from unittest.mock import AsyncMock, patch
        from polymarket_copier.core.monitor import _MAX_WS_RETRIES

        monitor = TradeMonitor(
            tracked_wallets=["0xABC"],
            on_trade=_noop_trade,
        )

        call_count = 0
        stop_after = _MAX_WS_RETRIES + 3  # well past the old exit point

        async def always_fail():
            nonlocal call_count
            call_count += 1
            if call_count >= stop_after:
                monitor._stop_event.set()  # let the loop exit cleanly
            raise ConnectionError("simulated WS drop")

        with patch.object(monitor, "_ws_connect_and_listen", side_effect=always_fail):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                await monitor._ws_loop()

        # Must have continued retrying past the old _MAX_WS_RETRIES hard stop.
        assert call_count >= stop_after
        assert monitor._ws_healthy is False

    @pytest.mark.asyncio
    async def test_ws_healthy_false_after_failures(self):
        # H10: ws_healthy stays False while WS is down; loop remains alive.
        from unittest.mock import AsyncMock, patch

        monitor = TradeMonitor(
            tracked_wallets=["0xABC"],
            on_trade=_noop_trade,
        )
        monitor._ws_healthy = True  # start as healthy
        calls = 0

        async def always_fail():
            nonlocal calls
            calls += 1
            if calls >= 3:
                monitor._stop_event.set()
            raise OSError("network gone")

        with patch.object(monitor, "_ws_connect_and_listen", side_effect=always_fail):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                await monitor._ws_loop()

        assert monitor._ws_healthy is False

    @pytest.mark.asyncio
    async def test_retry_count_resets_on_clean_reconnect(self):
        """A single successful connect resets the retry counter."""
        from unittest.mock import AsyncMock, patch

        monitor = TradeMonitor(
            tracked_wallets=["0xABC"],
            on_trade=_noop_trade,
        )
        monitor._stop_event.set()  # stop after first iteration

        async def succeed_once():
            pass  # clean return = successful connection

        with patch.object(monitor, "_ws_connect_and_listen", side_effect=succeed_once):
            await monitor._ws_loop()

        assert monitor._ws_retry_count == 0

    @pytest.mark.asyncio
    async def test_backoff_capped_at_ws_max_backoff(self):
        """H10: backoff is capped so we retry at most every ws_max_backoff_seconds."""
        from unittest.mock import AsyncMock, patch

        cap = 7.0
        monitor = TradeMonitor(
            tracked_wallets=["0xABC"],
            on_trade=_noop_trade,
            ws_max_backoff=cap,
        )
        sleep_calls = []

        async def fake_sleep(delay):
            sleep_calls.append(delay)

        calls = 0

        async def always_fail():
            nonlocal calls
            calls += 1
            if calls >= 8:
                monitor._stop_event.set()
            raise ConnectionError("drop")

        with patch.object(monitor, "_ws_connect_and_listen", side_effect=always_fail):
            with patch("asyncio.sleep", side_effect=fake_sleep):
                await monitor._ws_loop()

        # Every sleep call must respect the cap.
        assert all(d <= cap + 1e-6 for d in sleep_calls), sleep_calls


# ─── C5: set_wallets — rebalance without KeyError on new wallets ──────────────


class TestSetWallets:
    """C5 fix: set_wallets() must initialise _seen_trade_ids for newly-added wallets
    so that their first poll primes the baseline rather than KeyError-ing out."""

    def test_set_wallets_adds_seen_ids_for_new_wallet(self):
        monitor = TradeMonitor(tracked_wallets=["0xold"], on_trade=_noop_trade)
        monitor.set_wallets(["0xold", "0xnew"])
        assert "0xnew" in monitor._seen_trade_ids
        assert "0xold" in monitor._seen_trade_ids  # retained

    def test_set_wallets_replaces_wallet_list(self):
        monitor = TradeMonitor(tracked_wallets=["0xold"], on_trade=_noop_trade)
        monitor.set_wallets(["0xnew"])
        assert monitor._wallets == ["0xnew"]
        assert "0xold" not in monitor._wallets

    def test_set_wallets_lowercases(self):
        monitor = TradeMonitor(tracked_wallets=["0xabc"], on_trade=_noop_trade)
        monitor.set_wallets(["0xABC", "0xDEF"])
        assert monitor._wallets == ["0xabc", "0xdef"]

    def test_set_wallets_new_wallet_not_primed(self):
        """A wallet added via set_wallets must be unprimed so its first poll is a
        baseline seed (cold-start guard) rather than copying a backlog of trades."""
        monitor = TradeMonitor(tracked_wallets=["0xold"], on_trade=_noop_trade)
        monitor._primed_wallets.add("0xold")
        monitor.set_wallets(["0xold", "0xnew"])
        assert "0xnew" not in monitor._primed_wallets  # new → not primed
        assert "0xold" in monitor._primed_wallets  # existing priming retained

    def test_set_wallets_preserves_seen_ids_for_retained_wallet(self):
        monitor = TradeMonitor(tracked_wallets=["0xold"], on_trade=_noop_trade)
        from collections import OrderedDict

        monitor._seen_trade_ids["0xold"]["tx-seen"] = None
        monitor.set_wallets(["0xold", "0xnew"])
        # Existing seen ids must survive — losing them causes duplicate detection.
        assert "tx-seen" in monitor._seen_trade_ids["0xold"]

    def test_new_wallet_can_be_polled_without_keyerror(self):
        """After set_wallets, calling _filter_new_trades on the new wallet must
        not raise KeyError (the bug that C5 fixes)."""
        monitor = TradeMonitor(tracked_wallets=["0xold"], on_trade=_noop_trade)
        monitor.set_wallets(["0xold", "0xnew"])
        activity = [{"id": "t1", "type": "trade", "side": "BUY"}]
        # Must not raise — the pre-fix code KeyError'd here.
        result = monitor._filter_new_trades("0xnew", activity, prime=True)
        assert result == []  # prime=True → seeds baseline, returns nothing


# ─── H17: Poll-cadence jitter (front-run resistance) ──────────────────────────


class TestPollJitter:
    """H17: the poll cadence must be unpredictable so an observer can't time our
    detection and front-run the copy. _next_interval() adds bounded jitter and the
    per-wallet stagger decorrelates wallet timing."""

    def test_zero_jitter_returns_exact_interval(self):
        # poll_jitter=0 must be exactly periodic (legacy behaviour preserved).
        monitor = TradeMonitor(
            tracked_wallets=["0xWHALE"],
            on_trade=_noop_trade,
            poll_interval=8.0,
            poll_jitter=0.0,
        )
        for _ in range(20):
            assert monitor._next_interval() == 8.0

    def test_jitter_stays_within_bounds(self):
        # With jitter=2, every interval must lie in [interval-2, interval+2].
        monitor = TradeMonitor(
            tracked_wallets=["0xWHALE"],
            on_trade=_noop_trade,
            poll_interval=8.0,
            poll_jitter=2.0,
            jitter_seed=42,
        )
        samples = [monitor._next_interval() for _ in range(1000)]
        assert all(6.0 <= s <= 10.0 for s in samples)
        # Must actually vary — not a constant.
        assert len(set(samples)) > 100

    def test_jitter_mean_approximates_base_interval(self):
        # Symmetric jitter → mean stays close to the base interval (no systematic drift).
        monitor = TradeMonitor(
            tracked_wallets=["0xWHALE"],
            on_trade=_noop_trade,
            poll_interval=8.0,
            poll_jitter=2.0,
            jitter_seed=7,
        )
        samples = [monitor._next_interval() for _ in range(5000)]
        assert abs(sum(samples) / len(samples) - 8.0) < 0.15

    def test_jitter_never_below_floor(self):
        # Even with jitter larger than the interval, the floor protects against a
        # near-zero spin loop.
        monitor = TradeMonitor(
            tracked_wallets=["0xWHALE"],
            on_trade=_noop_trade,
            poll_interval=2.0,
            poll_jitter=10.0,
            jitter_seed=1,
        )
        samples = [monitor._next_interval() for _ in range(1000)]
        assert all(s >= 1.0 for s in samples)  # _MIN_POLL_FLOOR

    def test_seed_makes_jitter_deterministic(self):
        # Same seed → same sequence (reproducible tests, but still unpredictable
        # to an observer who doesn't know the seed; production uses seed=None).
        m1 = TradeMonitor(
            tracked_wallets=["0xW"], on_trade=_noop_trade, poll_interval=8.0, poll_jitter=2.0, jitter_seed=99
        )
        m2 = TradeMonitor(
            tracked_wallets=["0xW"], on_trade=_noop_trade, poll_interval=8.0, poll_jitter=2.0, jitter_seed=99
        )
        assert [m1._next_interval() for _ in range(50)] == [m2._next_interval() for _ in range(50)]

    @pytest.mark.asyncio
    async def test_staggered_poll_still_fetches_all_wallets(self):
        # The per-wallet stagger must not drop any wallet — all still get polled.
        polled = []

        async def capture_trade(event):
            return None

        monitor = TradeMonitor(
            tracked_wallets=["0xA", "0xB", "0xC"],
            on_trade=capture_trade,
            poll_jitter=0.01,
            jitter_seed=3,
            prime_on_start=False,
        )

        async def fake_poll(session, wallet):
            polled.append(wallet)

        monitor._poll_wallet = fake_poll
        await monitor._poll_all_wallets(_FakeSessionMonitor([]))
        assert sorted(polled) == ["0xa", "0xb", "0xc"]

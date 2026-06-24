"""Chaos / adversarial tests.

These tests inject network errors, malformed payloads, and concurrency pressure to
verify that the bot degrades gracefully rather than crashing, leaking exposure, or
placing duplicate orders. Nothing here tests the happy path — that belongs in the
unit and integration test suites.

Categories:
  TestAPIErrorResilience  — HTTP 500/429, empty responses, malformed trade records
  TestWSChaos            — invalid JSON, out-of-range prices, extreme price jumps
  TestConcurrencyInvariants — concurrent entries on the same token, concurrent exits
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from polymarket_copier.api.clob_client import ClobClient
from polymarket_copier.config import AppConfig
from polymarket_copier.core.copier import CopyTrader
from polymarket_copier.core.monitor import TradeMonitor
from polymarket_copier.core.portfolio import PortfolioManager
from polymarket_copier.core.risk_manager import RiskConfig, RiskManager
from polymarket_copier.models.types import Market


# ─── Fake HTTP helpers ────────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, data=None, status=200):
        self._data = data if data is not None else []
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._data


class _FakeSession:
    def __init__(self, data=None, status=200):
        self._data = data if data is not None else []
        self._status = status

    def get(self, url, params=None):
        return _FakeResp(self._data, self._status)


# ─── Shared fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def config() -> AppConfig:
    cfg = AppConfig(mode="paper", bankroll=10_000)
    cfg.copy_trading.max_trade_age_seconds = 0  # disable staleness gate in tests
    return cfg


@pytest.fixture
async def portfolio(tmp_path):
    pm = PortfolioManager(db_path=str(tmp_path / "chaos.db"))
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
def wired(config, portfolio, gamma):
    """Fully wired monitor → copier graph (paper mode, no WS)."""
    risk = RiskManager(config=RiskConfig(), bankroll=config.bankroll)
    clob = ClobClient(config)
    copier = CopyTrader(risk, portfolio, clob, gamma, config)
    monitor = TradeMonitor(
        tracked_wallets=["0xwhale"],
        on_trade=copier.handle_trade_event,
        on_price=copier.handle_price_tick,
        prime_on_start=False,
    )
    copier.monitor = monitor
    return monitor, copier


BUY_ACTIVITY = [{
    "id": "trade-1", "type": "trade", "side": "BUY",
    "market": "mkt-a", "asset": "tok-a",
    "price": "0.50", "size": "100", "timestamp": 1_700_000_000,
}]


# ─── TestAPIErrorResilience ────────────────────────────────────────────────────

class TestAPIErrorResilience:
    """The poll loop must survive any HTTP error without crashing."""

    async def test_poll_http_500_logs_warning_no_crash(self, wired, caplog):
        """An HTTP 500 from the Data API must log a warning and return without crashing
        the poll loop or raising an unhandled exception."""
        monitor, _copier = wired
        session = _FakeSession(status=500)

        with caplog.at_level(logging.WARNING):
            await monitor._poll_wallet(session, "0xwhale")  # must not raise

        assert await _copier.portfolio.position_count() == 0
        assert any("500" in rec.message for rec in caplog.records), (
            "Expected a warning log containing the HTTP status code 500."
        )

    async def test_poll_http_429_logs_warning_no_crash(self, wired, caplog):
        """An HTTP 429 (rate-limit) response is treated the same as any other non-200:
        log a warning and skip the cycle without crashing."""
        monitor, _copier = wired
        session = _FakeSession(status=429)

        with caplog.at_level(logging.WARNING):
            await monitor._poll_wallet(session, "0xwhale")

        assert await _copier.portfolio.position_count() == 0
        assert any("429" in rec.message for rec in caplog.records)

    async def test_empty_api_response_produces_no_trade_events(self, wired):
        """An empty list from the Data API (no recent activity) must not trigger any copies."""
        monitor, copier = wired
        session = _FakeSession(data=[])

        await monitor._poll_wallet(session, "0xwhale")

        assert await copier.portfolio.position_count() == 0

    async def test_malformed_trade_in_response_is_skipped(self, wired, caplog):
        """A trade record missing required fields (price, size, market) must be skipped
        with a warning rather than crashing or opening a position."""
        monitor, copier = wired
        malformed = [
            {"id": "bad-1", "type": "trade", "side": "BUY"},  # missing price/size/market
        ]
        session = _FakeSession(data=malformed)

        with caplog.at_level(logging.WARNING):
            await monitor._poll_wallet(session, "0xwhale")

        assert await copier.portfolio.position_count() == 0

    async def test_non_trade_activity_items_are_ignored(self, wired):
        """Activity records with type != 'trade'/'buy'/'sell' (e.g. 'deposit') must be
        filtered out by _filter_new_trades and not emit any TradeEvent."""
        monitor, copier = wired
        non_trade = [
            {"id": "dep-1", "type": "deposit", "amount": "1000"},
        ]
        session = _FakeSession(data=non_trade)

        await monitor._poll_wallet(session, "0xwhale")

        assert await copier.portfolio.position_count() == 0

    async def test_duplicate_trade_ids_copied_only_once(self, wired):
        """Two poll cycles returning the same trade ID must open exactly one position
        (deduplication via _seen_trade_ids)."""
        monitor, copier = wired
        session = _FakeSession(BUY_ACTIVITY)

        await monitor._poll_wallet(session, "0xwhale")
        await monitor._poll_wallet(session, "0xwhale")

        assert await copier.portfolio.position_count() == 1


# ─── TestWSChaos ──────────────────────────────────────────────────────────────

class TestWSChaos:
    """The WS message handler must be robust to malformed input and extreme prices."""

    async def test_ws_invalid_json_no_crash(self, wired, caplog):
        """A WebSocket message that is not valid JSON must be handled gracefully
        (caught, logged as a warning) without crashing the message loop."""
        monitor, _copier = wired
        monitor._subscribed_tokens.add("tok-x")

        with caplog.at_level(logging.WARNING):
            try:
                await monitor._handle_ws_message("NOT JSON {{")
            except Exception as exc:
                pytest.fail(f"_handle_ws_message raised unexpectedly: {exc}")

    async def test_ws_price_above_1_filtered_no_tick(self, wired):
        """A WS price > 1.0 (outside the valid Polymarket range) must be silently
        dropped — no PriceTick emitted, no position opened via the on_price callback."""
        monitor, copier = wired
        monitor._subscribed_tokens.add("tok-a")
        # Open a position first so the subscription is active.
        session = _FakeSession(BUY_ACTIVITY)
        await monitor._poll_wallet(session, "0xwhale")

        ticks_received = []
        copier.handle_price_tick_orig = copier.handle_price_tick  # save original

        async def capturing_price_tick(tick):
            ticks_received.append(tick)
            await copier.handle_price_tick_orig(tick)

        monitor._on_price = capturing_price_tick

        raw = json.dumps([{"event_type": "price_change", "asset_id": "tok-a", "price": "1.50"}])
        await monitor._handle_ws_message(raw)

        assert len(ticks_received) == 0, (
            "An out-of-range price (1.50) must be filtered before emitting a PriceTick."
        )

    async def test_ws_price_below_0_filtered_no_tick(self, wired):
        """A WS price < 0.0 must also be filtered."""
        monitor, copier = wired
        monitor._subscribed_tokens.add("tok-a")
        session = _FakeSession(BUY_ACTIVITY)
        await monitor._poll_wallet(session, "0xwhale")

        ticks_received = []
        copier.handle_price_tick_orig = copier.handle_price_tick

        async def capturing_price_tick(tick):
            ticks_received.append(tick)
            await copier.handle_price_tick_orig(tick)

        monitor._on_price = capturing_price_tick

        raw = json.dumps([{"event_type": "price_change", "asset_id": "tok-a", "price": "-0.10"}])
        await monitor._handle_ws_message(raw)

        assert len(ticks_received) == 0

    async def test_ws_unsubscribed_token_price_ignored(self, wired):
        """A WS price tick for a token NOT in _subscribed_tokens must not trigger
        any callback (the token is not a position we hold)."""
        monitor, copier = wired
        ticks_received = []

        async def tracking_price_callback(tick):
            ticks_received.append(tick)

        monitor._on_price = tracking_price_callback
        # Deliberately do NOT subscribe "tok-other".

        raw = json.dumps([{"event_type": "price_change", "asset_id": "tok-other", "price": "0.90"}])
        await monitor._handle_ws_message(raw)

        assert len(ticks_received) == 0

    async def test_ws_price_jump_to_tp_exits_position(self, wired):
        """A WS price tick that jumps straight past the TP threshold must trigger
        an immediate exit. This simulates a sudden market resolution or liquidity spike.

        Entry=0.50 → TP ≈ 0.70. A tick at 0.99 is well above TP.
        """
        monitor, copier = wired
        session = _FakeSession(BUY_ACTIVITY)
        await monitor._poll_wallet(session, "0xwhale")
        assert await copier.portfolio.position_count() == 1

        tp_jump = json.dumps([
            {"event_type": "price_change", "asset_id": "tok-a", "price": "0.99"}
        ])
        await monitor._handle_ws_message(tp_jump)

        assert await copier.portfolio.position_count() == 0, (
            "Position was not closed on a WS price jump past TP (0.99 >> 0.70)."
        )

    async def test_ws_price_crash_to_sl_exits_position(self, wired):
        """A WS price tick that crashes below the SL threshold must trigger an exit.

        Entry=0.50 → SL ≈ 0.50 - 0.50*0.25 = 0.375. A tick at 0.10 is well below SL.
        """
        monitor, copier = wired
        session = _FakeSession(BUY_ACTIVITY)
        await monitor._poll_wallet(session, "0xwhale")
        assert await copier.portfolio.position_count() == 1

        sl_crash = json.dumps([
            {"event_type": "price_change", "asset_id": "tok-a", "price": "0.10"}
        ])
        await monitor._handle_ws_message(sl_crash)

        assert await copier.portfolio.position_count() == 0, (
            "Position was not closed on a WS price crash below SL (0.10 << 0.375)."
        )

    async def test_ws_multiple_event_types_in_one_message(self, wired):
        """A WS message containing both a price_change and an unknown event type
        for the same token must process the price_change and ignore the unknown."""
        monitor, copier = wired
        session = _FakeSession(BUY_ACTIVITY)
        await monitor._poll_wallet(session, "0xwhale")

        mixed = json.dumps([
            {"event_type": "order_placed", "asset_id": "tok-a"},   # unknown → ignored
            {"event_type": "price_change", "asset_id": "tok-a", "price": "0.99"},  # TP hit
        ])
        await monitor._handle_ws_message(mixed)

        assert await copier.portfolio.position_count() == 0


# ─── TestConcurrencyInvariants ────────────────────────────────────────────────

class TestConcurrencyInvariants:
    """Concurrent wallet polls and exit triggers must not corrupt positions or exposure."""

    async def test_concurrent_polls_two_wallets_independent_positions(self, config, tmp_path, gamma):
        """Two wallets buying different tokens simultaneously each open exactly one position.
        No interference between concurrent poll tasks."""
        portfolio_a = PortfolioManager(db_path=str(tmp_path / "conc.db"))
        await portfolio_a.init()

        risk = RiskManager(config=RiskConfig(), bankroll=config.bankroll)
        clob = ClobClient(config)
        copier = CopyTrader(risk, portfolio_a, clob, gamma, config)

        # Two wallets, two different markets/tokens.
        monitor = TradeMonitor(
            tracked_wallets=["0xwhale1", "0xwhale2"],
            on_trade=copier.handle_trade_event,
            on_price=copier.handle_price_tick,
            prime_on_start=False,
        )
        copier.monitor = monitor

        gamma.get_market = AsyncMock(side_effect=lambda mid: Market(
            condition_id=mid, question="Q?", volume_24h=50_000, active=True, resolve_time=None,
        ))

        activity_w1 = [{"id": "t-w1", "type": "trade", "side": "BUY",
                        "market": "mkt-1", "asset": "tok-1",
                        "price": "0.50", "size": "50", "timestamp": 1_700_000_000}]
        activity_w2 = [{"id": "t-w2", "type": "trade", "side": "BUY",
                        "market": "mkt-2", "asset": "tok-2",
                        "price": "0.50", "size": "50", "timestamp": 1_700_000_000}]

        session_w1 = _FakeSession(activity_w1)
        session_w2 = _FakeSession(activity_w2)

        await asyncio.gather(
            monitor._poll_wallet(session_w1, "0xwhale1"),
            monitor._poll_wallet(session_w2, "0xwhale2"),
        )

        assert await portfolio_a.position_count() == 2, (
            "Each of the two wallets should have opened exactly one independent position."
        )
        await portfolio_a.close()

    async def test_concurrent_polls_same_token_cap_respected(self, config, tmp_path, gamma):
        """Two wallets trading the SAME token concurrently must not exceed
        max_positions_per_token (default 3). With only two wallets here, both
        should open (2 ≤ 3) — but the entry lock must prevent a TOCTOU race
        that could open the same trade twice from the same wallet."""
        pm = PortfolioManager(db_path=str(tmp_path / "cap.db"))
        await pm.init()

        risk = RiskManager(config=RiskConfig(), bankroll=config.bankroll)
        clob = ClobClient(config)
        copier = CopyTrader(risk, pm, clob, gamma, config)
        monitor = TradeMonitor(
            tracked_wallets=["0xwhale1", "0xwhale2"],
            on_trade=copier.handle_trade_event,
            on_price=copier.handle_price_tick,
            prime_on_start=False,
        )
        copier.monitor = monitor

        # Both wallets trade the SAME token on the SAME market.
        same_token_activity_w1 = [{"id": "t1-w1", "type": "trade", "side": "BUY",
                                    "market": "mkt-shared", "asset": "tok-shared",
                                    "price": "0.50", "size": "50", "timestamp": 1_700_000_000}]
        same_token_activity_w2 = [{"id": "t1-w2", "type": "trade", "side": "BUY",
                                    "market": "mkt-shared", "asset": "tok-shared",
                                    "price": "0.50", "size": "50", "timestamp": 1_700_000_000}]
        s1 = _FakeSession(same_token_activity_w1)
        s2 = _FakeSession(same_token_activity_w2)

        await asyncio.gather(
            monitor._poll_wallet(s1, "0xwhale1"),
            monitor._poll_wallet(s2, "0xwhale2"),
        )

        count = await pm.position_count()
        assert count <= config.copy_trading.max_positions_per_token, (
            f"Opened {count} positions on the same token, exceeding per-token cap "
            f"of {config.copy_trading.max_positions_per_token}."
        )
        await pm.close()

    async def test_same_wallet_duplicate_poll_opens_one_position(self, wired):
        """The same trade event received in two consecutive polls (same wallet, same trade ID)
        must be deduplicated by _seen_trade_ids so only ONE position is opened."""
        monitor, copier = wired
        session = _FakeSession(BUY_ACTIVITY)

        await monitor._poll_wallet(session, "0xwhale")
        await monitor._poll_wallet(session, "0xwhale")

        assert await copier.portfolio.position_count() == 1

    async def test_concurrent_exits_different_positions_no_crash(self, config, tmp_path, gamma):
        """Concurrent asyncio.gather over check_all_exits for independent positions
        must complete without exceptions and close all positions."""
        pm = PortfolioManager(db_path=str(tmp_path / "multi_exit.db"))
        await pm.init()

        gamma.get_market_price = AsyncMock(return_value=0.90)  # above TP=0.70

        risk = RiskManager(config=RiskConfig(), bankroll=config.bankroll)
        clob = ClobClient(config)
        copier = CopyTrader(risk, pm, clob, gamma, config)
        copier.monitor = MagicMock()
        copier.monitor.unsubscribe_token = MagicMock()

        # Open two positions on different tokens.
        for i in range(2):
            pos = await risk.build_position(
                position_id=f"pos-multi-{i}",
                market_id=f"mkt-multi-{i}",
                token_id=f"tok-multi-{i}",
                trader_address="0xwhale",
                entry_price=0.50,
                size_shares=5.0,
            )
            await pm.open_position(pos)

        assert await pm.position_count() == 2

        # Two concurrent exit sweeps.
        await asyncio.gather(
            copier.check_all_exits(),
            copier.check_all_exits(),
        )

        # All positions must be closed (0 open) — at most 2 exits each run once.
        assert await pm.position_count() == 0

        await pm.close()

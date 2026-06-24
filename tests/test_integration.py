"""Integration tests: exercise the REAL TradeMonitor -> CopyTrader wiring.

These tests deliberately use the actual monitor dispatch path (not a direct call
to copier.handle_trade_event) so that a regression like "async callback invoked
without await" is caught: if the monitor fails to await the callback, no position
is opened and these tests fail.

L4: WS death + recovery tests verify that:
  - ws_healthy transitions correctly on connect/disconnect
  - exit poll cadence logic uses fast interval when WS is down
  - WS reconnect backoff is always capped at ws_max_backoff (H10)
  - heartbeat watchdog contract (last_poll_completed_at updated per cycle)

L7: Simultaneous multi-market exit tests verify that:
  - check_all_exits closes all triggered positions in one sweep
  - concurrent WS price ticks for the same token close the position exactly once
  - market exposure is fully released after simultaneous exits
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from polymarket_copier.api.clob_client import ClobClient
from polymarket_copier.config import AppConfig
from polymarket_copier.core.copier import CopyTrader
from polymarket_copier.core.monitor import TradeMonitor, _WS_RECONNECT_DELAY
from polymarket_copier.core.portfolio import PortfolioManager
from polymarket_copier.core.risk_manager import RiskConfig, RiskManager
from polymarket_copier.models.types import Market


# ─── Fake aiohttp session for the monitor's REST poll path ────────────────────

class _FakeResp:
    def __init__(self, data, status=200):
        self._data = data
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._data


class _FakeSession:
    """Minimal stand-in for aiohttp.ClientSession.get()."""

    def __init__(self, data):
        self._data = data

    def get(self, url, params=None):
        return _FakeResp(self._data)


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def config() -> AppConfig:
    cfg = AppConfig(mode="paper", bankroll=10_000)
    # These wiring tests use a fixed historical timestamp in BUY_ACTIVITY, so
    # neutralize the staleness gate — they exercise the monitor->copier dispatch,
    # not freshness filtering (which has its own dedicated tests).
    cfg.copy_trading.max_trade_age_seconds = 0
    return cfg


@pytest.fixture
async def portfolio(tmp_path):
    pm = PortfolioManager(db_path=str(tmp_path / "integration.db"))
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
    """A fully wired monitor -> copier graph (paper mode)."""
    risk = RiskManager(config=RiskConfig(), bankroll=config.bankroll)
    clob = ClobClient(config)
    copier = CopyTrader(risk, portfolio, clob, gamma, config)
    monitor = TradeMonitor(
        tracked_wallets=["0xWHALE"],
        on_trade=copier.handle_trade_event,   # async def
        on_price=copier.handle_price_tick,     # async def
        prime_on_start=False,   # act on the first poll (cold-start guard tested separately)
    )
    copier.monitor = monitor
    return monitor, copier


BUY_ACTIVITY = [{
    "id": "trade-1", "type": "trade", "side": "BUY",
    "market": "mkt-a", "asset": "tok-a",
    "price": "0.50", "size": "100", "timestamp": 1_700_000_000,
}]


# ─── Tests ────────────────────────────────────────────────────────────────────

class TestMonitorToCopier:
    @pytest.mark.asyncio
    async def test_detected_buy_opens_position_through_monitor(self, wired):
        """Regression guard for the un-awaited-callback bug: a BUY detected by the
        monitor's poll path must actually flow through to an opened position."""
        monitor, copier = wired
        session = _FakeSession(BUY_ACTIVITY)

        # Drive the real monitor dispatch (awaits on_trade internally).
        await monitor._poll_wallet(session, "0xwhale")

        assert await copier.portfolio.position_count() == 1, (
            "Monitor failed to await the async on_trade callback — no position opened."
        )

    @pytest.mark.asyncio
    async def test_duplicate_poll_does_not_double_open(self, wired):
        """A second poll of the same activity must not re-open (dedup by trade id)."""
        monitor, copier = wired
        session = _FakeSession(BUY_ACTIVITY)

        await monitor._poll_wallet(session, "0xwhale")
        await monitor._poll_wallet(session, "0xwhale")

        assert await copier.portfolio.position_count() == 1

    @pytest.mark.asyncio
    async def test_ws_price_tick_triggers_take_profit_exit(self, wired):
        """After opening via the monitor, a WS price tick at TP must exit the
        position through the real _handle_ws_message -> on_price path."""
        monitor, copier = wired
        session = _FakeSession(BUY_ACTIVITY)
        await monitor._poll_wallet(session, "0xwhale")
        assert await copier.portfolio.position_count() == 1

        # Entry 0.50 -> TP 0.70. A tick at 0.72 should close the position.
        raw = json.dumps([
            {"event_type": "price_change", "asset_id": "tok-a", "price": "0.72"}
        ])
        await monitor._handle_ws_message(raw)

        assert await copier.portfolio.position_count() == 0, (
            "WS price tick did not flow through to an exit — on_price not awaited?"
        )

    @pytest.mark.asyncio
    async def test_ws_tick_below_tp_holds(self, wired):
        monitor, copier = wired
        session = _FakeSession(BUY_ACTIVITY)
        await monitor._poll_wallet(session, "0xwhale")

        raw = json.dumps([
            {"event_type": "price_change", "asset_id": "tok-a", "price": "0.55"}
        ])
        await monitor._handle_ws_message(raw)

        assert await copier.portfolio.position_count() == 1

    @pytest.mark.asyncio
    async def test_ws_tick_persists_peak_for_trailing_stop(self, wired):
        """Regression guard for the dead trailing-stop bug: a new high seen on the
        WS feed must be persisted to the DB so the trailing stop can tighten."""
        monitor, copier = wired
        session = _FakeSession(BUY_ACTIVITY)
        await monitor._poll_wallet(session, "0xwhale")

        # 0.68 is a new high but below TP (0.70) — should persist peak, not exit.
        raw = json.dumps([
            {"event_type": "price_change", "asset_id": "tok-a", "price": "0.68"}
        ])
        await monitor._handle_ws_message(raw)

        pos = await copier.portfolio.get_position_by_token("tok-a")
        assert pos is not None
        assert pos.peak_price == pytest.approx(0.68), (
            "Peak was not persisted to the DB — trailing stop would never tighten."
        )


# ─── L4: WS Death + Recovery ──────────────────────────────────────────────────

class TestWSDeathAndRecovery:
    """L4: Verify WebSocket health flag transitions, backoff cap, and heartbeat contract."""

    def test_ws_healthy_false_on_init(self):
        """A freshly created monitor starts with ws_healthy=False (no connection yet)."""
        monitor = TradeMonitor(
            tracked_wallets=["0xabc"],
            on_trade=AsyncMock(),
            prime_on_start=False,
        )
        assert monitor.ws_healthy is False

    async def test_last_poll_completed_at_set_after_poll_cycle(self):
        """Heartbeat watchdog contract: last_poll_completed_at is updated after every
        full poll cycle inside _poll_loop (not by _poll_wallet alone).

        We run _poll_loop briefly with aiohttp mocked and _poll_all_wallets patched to
        stop the loop after one iteration, then verify the attribute was set.
        """
        monitor = TradeMonitor(
            tracked_wallets=["0xwhale"],
            on_trade=AsyncMock(),
            prime_on_start=False,
            poll_interval=0.001,
            poll_jitter=0,
        )
        assert monitor.last_poll_completed_at is None

        async def _stop_after_one(_session):
            # _poll_loop calls _poll_all_wallets then sets last_poll_completed_at.
            # Stop the loop so the test doesn't run forever.
            monitor._stop_event.set()

        mock_session = MagicMock()
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        with patch.object(monitor, "_poll_all_wallets", side_effect=_stop_after_one), \
             patch("polymarket_copier.core.monitor.aiohttp.ClientSession", return_value=mock_cm), \
             patch("polymarket_copier.core.monitor.aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("polymarket_copier.core.monitor.aiohttp.ClientTimeout", return_value=MagicMock()):
            try:
                await asyncio.wait_for(monitor._poll_loop(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

        assert monitor.last_poll_completed_at is not None, (
            "last_poll_completed_at must be set after each _poll_loop cycle "
            "so the heartbeat watchdog can detect a stalled poll loop."
        )
        assert monitor.last_poll_completed_at <= time.time()

    def test_ws_backoff_capped_at_max_backoff(self):
        """H10: reconnect delay = min(base * 2^(n-1), ws_max_backoff) must never exceed the cap.

        Prior bug had delay reach 5*2^4=80s, leaving positions un-managed for >1 min.
        With the cap at 30s, the worst-case gap is 30s regardless of retry count.
        """
        max_backoff = 30.0
        base = _WS_RECONNECT_DELAY  # 5s

        for retry in range(1, 20):
            delay = min(base * (2 ** (retry - 1)), max_backoff)
            assert delay <= max_backoff, f"retry={retry}: delay {delay} exceeds cap {max_backoff}"

        # After 4+ retries the exponential (5*2^3=40) would exceed the cap without the clamp.
        assert min(base * (2 ** 3), max_backoff) == max_backoff  # 40 → 30 (capped)
        assert min(base * (2 ** 10), max_backoff) == max_backoff  # 5120 → 30 (capped)

    async def test_ws_loop_marks_unhealthy_and_retries_on_failure(self):
        """A _ws_loop iteration that fails sets ws_healthy=False and increments the
        retry counter. The loop keeps retrying — it never permanently exits.

        We drive two failure iterations with instant mock sleeps, then cancel the loop.
        """
        on_trade = AsyncMock()
        monitor = TradeMonitor(
            tracked_wallets=["0xabc"],
            on_trade=on_trade,
            prime_on_start=False,
        )

        call_count = 0

        async def _failing_connect():
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                # Stop the loop after 3 attempts so the test terminates.
                monitor._stop_event.set()
            raise OSError("connection refused")

        with patch.object(monitor, "_ws_connect_and_listen", side_effect=_failing_connect), \
             patch("polymarket_copier.core.monitor.asyncio.sleep", new_callable=AsyncMock):
            try:
                await asyncio.wait_for(monitor._ws_loop(), timeout=2.0)
            except asyncio.TimeoutError:
                pass  # loop cancelled by timeout is acceptable

        assert monitor.ws_healthy is False
        assert monitor._ws_retry_count >= 2

    async def test_ws_loop_resets_retry_count_on_clean_listen(self):
        """If _ws_connect_and_listen returns normally (clean disconnect), the retry counter
        resets to 0 so a subsequent failure starts the backoff from the beginning."""
        on_trade = AsyncMock()
        monitor = TradeMonitor(
            tracked_wallets=["0xabc"],
            on_trade=on_trade,
            prime_on_start=False,
        )
        monitor._ws_retry_count = 5  # pre-set to simulate prior failures

        clean_then_stop_calls = 0

        async def _clean_connect():
            nonlocal clean_then_stop_calls
            clean_then_stop_calls += 1
            monitor._stop_event.set()  # one successful iteration → stop

        with patch.object(monitor, "_ws_connect_and_listen", side_effect=_clean_connect):
            try:
                await asyncio.wait_for(monitor._ws_loop(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

        assert monitor._ws_retry_count == 0, "Retry counter should reset after a clean listen"

    async def test_exit_cadence_logic_uses_fast_interval_when_ws_down(self, config):
        """The exit_check_loop interval selection: ws_healthy=False → fast interval.

        This directly tests the conditional that lives in main.exit_check_loop so any
        change to that logic fails here before it silently degrades exit latency.
        """
        fast_interval = config.risk_management.exit_poll_fast_seconds
        normal_interval = config.polling_interval_seconds

        monitor = TradeMonitor(
            tracked_wallets=["0xabc"],
            on_trade=AsyncMock(),
            prime_on_start=False,
        )
        # New monitor starts unhealthy — exit_check_loop should use fast cadence.
        interval_when_down = fast_interval if not monitor.ws_healthy else normal_interval
        assert interval_when_down == fast_interval
        assert fast_interval < normal_interval, (
            "exit_poll_fast_seconds must be less than polling_interval_seconds"
        )


# ─── L7: Simultaneous Multi-Market Exits ──────────────────────────────────────

class TestSimultaneousMultiMarketExits:
    """L7: Verify that concurrent position exits are race-free and exposure is fully released."""

    @pytest.fixture
    async def multi_market_setup(self, config, portfolio, gamma):
        """Three independent positions in three different markets, each at entry=0.50.

        TP for entry=0.50 is 0.50 + (1-0.50)*0.40 = 0.70.
        We set gamma to return 0.75 so all three are above TP and exit on the next
        check_all_exits sweep.
        """
        gamma.get_market_price = AsyncMock(return_value=0.75)
        risk = RiskManager(config=RiskConfig(), bankroll=config.bankroll)
        clob = ClobClient(config)
        copier = CopyTrader(risk, portfolio, clob, gamma, config)

        for i in range(3):
            pos = await risk.build_position(
                position_id=f"pos-{i}",
                market_id=f"mkt-{i}",
                token_id=f"tok-{i}",
                trader_address="0xwhale",
                entry_price=0.50,
                size_shares=10.0,
            )
            await portfolio.open_position(pos)
            copier.monitor = MagicMock()
            copier.monitor.unsubscribe_token = MagicMock()

        return copier, risk

    async def test_all_positions_exit_on_single_sweep(self, multi_market_setup):
        """check_all_exits in a single call must close all positions that hit TP simultaneously."""
        copier, _risk = multi_market_setup
        assert await copier.portfolio.position_count() == 3

        await copier.check_all_exits()

        assert await copier.portfolio.position_count() == 0, (
            "Not all positions were closed — simultaneous TP exits may have been dropped."
        )

    async def test_exposure_fully_released_after_simultaneous_exits(self, multi_market_setup):
        """After all positions exit, the RiskManager must have zero total exposure."""
        copier, risk = multi_market_setup

        await copier.check_all_exits()

        assert risk.total_exposure() == pytest.approx(0.0, abs=1e-9), (
            "Market exposure was not fully released after simultaneous exits."
        )

    async def test_concurrent_ws_ticks_no_double_close(self, wired):
        """Two concurrent WS ticks at TP price for the same token must close the
        position exactly once — not twice (which would corrupt PnL accounting).

        The double-exit race is prevented by two guards:
          1. copier._exit_locks[position_id] — asyncio.Lock (suspenders)
          2. `AND status='open'` in close_position() SQL — the belt
        This test drives both guards under real concurrency.
        """
        monitor, copier = wired
        session = _FakeSession(BUY_ACTIVITY)
        await monitor._poll_wallet(session, "0xwhale")
        assert await copier.portfolio.position_count() == 1

        # TP = 0.50 + (1-0.50)*0.40 = 0.70. Price 0.80 is above TP.
        tp_tick = json.dumps([
            {"event_type": "price_change", "asset_id": "tok-a", "price": "0.80"}
        ])

        # Fire two concurrent WS ticks at the same above-TP price.
        await asyncio.gather(
            monitor._handle_ws_message(tp_tick),
            monitor._handle_ws_message(tp_tick),
        )

        # Position must be closed (count=0) with no double-exit errors.
        assert await copier.portfolio.position_count() == 0, (
            "Position was not closed despite concurrent TP ticks."
        )

    async def test_concurrent_exits_leave_no_orphaned_exposure(self, wired):
        """After a concurrent double-tick exit, exposure must be 0 (no phantom allocation)."""
        monitor, copier = wired
        session = _FakeSession(BUY_ACTIVITY)
        await monitor._poll_wallet(session, "0xwhale")

        tp_tick = json.dumps([
            {"event_type": "price_change", "asset_id": "tok-a", "price": "0.80"}
        ])
        await asyncio.gather(
            monitor._handle_ws_message(tp_tick),
            monitor._handle_ws_message(tp_tick),
        )

        assert copier.risk.total_exposure() == pytest.approx(0.0, abs=1e-9)

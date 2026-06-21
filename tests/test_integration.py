"""Integration tests: exercise the REAL TradeMonitor -> CopyTrader wiring.

These tests deliberately use the actual monitor dispatch path (not a direct call
to copier.handle_trade_event) so that a regression like "async callback invoked
without await" is caught: if the monitor fails to await the callback, no position
is opened and these tests fail.
"""

from __future__ import annotations

import json

import pytest
from unittest.mock import AsyncMock

from polymarket_copier.api.clob_client import ClobClient
from polymarket_copier.config import AppConfig
from polymarket_copier.core.copier import CopyTrader
from polymarket_copier.core.monitor import TradeMonitor
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

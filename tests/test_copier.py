"""Tests for the v2 copy-trade engine (paper-mode orchestration)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from polymarket_copier.api.clob_client import ClobClient
from polymarket_copier.config import AppConfig
from polymarket_copier.core.copier import CopyTrader
from polymarket_copier.core.monitor import TradeEvent, TradeType
from polymarket_copier.core.portfolio import PortfolioManager
from polymarket_copier.core.risk_manager import RiskConfig, RiskManager
from polymarket_copier.models.types import Market


@pytest.fixture
def config() -> AppConfig:
    return AppConfig(mode="paper", bankroll=10_000)


@pytest.fixture
async def portfolio(tmp_path):
    pm = PortfolioManager(db_path=str(tmp_path / "copier_test.db"))
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
def copier(config, portfolio, gamma):
    risk = RiskManager(config=RiskConfig(), bankroll=config.bankroll)
    clob = ClobClient(config)
    return CopyTrader(risk, portfolio, clob, gamma, config)


def buy_event(price=0.50, size=100.0, market="mkt-a", token="tok-a", wallet="0xwhale") -> TradeEvent:
    return TradeEvent(
        event_id="e1", wallet_address=wallet, market_id=market, token_id=token,
        outcome_label="Yes", trade_type=TradeType.BUY, price=price,
        size_usdc=size, timestamp=time.time(), transaction_hash="0xhash",
    )


class TestHandleTradeEvent:
    @pytest.mark.asyncio
    async def test_buy_opens_position(self, copier):
        await copier.handle_trade_event(buy_event())
        assert await copier.portfolio.position_count() == 1

    @pytest.mark.asyncio
    async def test_copy_size_is_conservative(self, copier):
        # size_multiplier 0.5 → 50 USDC, well under 2% bankroll cap ($200)
        await copier.handle_trade_event(buy_event(price=0.50, size=100.0))
        positions = await copier.portfolio.get_open_positions()
        assert len(positions) == 1
        # 50 USDC / 0.50 price = 100 shares
        assert positions[0].size_shares == pytest.approx(100.0)

    @pytest.mark.asyncio
    async def test_copy_size_capped_at_bankroll_pct(self, copier):
        # Large source trade → capped at 2% of $10k = $200 → 400 shares @ 0.50
        await copier.handle_trade_event(buy_event(price=0.50, size=100_000.0))
        positions = await copier.portfolio.get_open_positions()
        assert positions[0].size_shares == pytest.approx(400.0)

    @pytest.mark.asyncio
    async def test_sell_event_skipped(self, copier):
        event = buy_event()
        sell = TradeEvent(**{**event.__dict__, "trade_type": TradeType.SELL})
        await copier.handle_trade_event(sell)
        assert await copier.portfolio.position_count() == 0

    @pytest.mark.asyncio
    async def test_price_deviation_skip(self, copier, gamma):
        # Current price 0.60 vs event 0.50 → 20% deviation > 2% max
        gamma.get_market_price = AsyncMock(return_value=0.60)
        await copier.handle_trade_event(buy_event(price=0.50))
        assert await copier.portfolio.position_count() == 0

    @pytest.mark.asyncio
    async def test_low_volume_skip(self, copier, gamma):
        gamma.get_market = AsyncMock(return_value=Market(
            condition_id="mkt-a", volume_24h=100, active=True, resolve_time=None,
        ))
        await copier.handle_trade_event(buy_event())
        assert await copier.portfolio.position_count() == 0

    @pytest.mark.asyncio
    async def test_resolution_blackout_skip(self, copier, gamma):
        from datetime import datetime, timezone, timedelta
        soon = datetime.now(timezone.utc) + timedelta(hours=6)
        gamma.get_market = AsyncMock(return_value=Market(
            condition_id="mkt-a", volume_24h=50_000, active=True, resolve_time=soon,
        ))
        await copier.handle_trade_event(buy_event())
        assert await copier.portfolio.position_count() == 0

    @pytest.mark.asyncio
    async def test_max_concurrent_positions(self, copier):
        copier.config.copy_trading.max_concurrent_positions = 1
        await copier.handle_trade_event(buy_event(market="mkt-a", token="tok-a"))
        await copier.handle_trade_event(buy_event(market="mkt-b", token="tok-b"))
        assert await copier.portfolio.position_count() == 1


class TestOrderFailureExposureRelease:
    """When a copy order fails after exposure is reserved, that reservation must
    be released — otherwise a never-opened position permanently consumes the
    per-market exposure cap and silently blocks future copies in that market."""

    @pytest.mark.asyncio
    async def test_generic_order_failure_releases_exposure(self, copier):
        copier.clob.place_order = AsyncMock(side_effect=RuntimeError("exchange down"))
        await copier.handle_trade_event(buy_event(market="mkt-x", token="tok-x"))
        assert await copier.portfolio.position_count() == 0
        assert copier.risk.market_exposure("mkt-x") == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_insufficient_liquidity_releases_exposure(self, copier):
        from polymarket_copier.api.clob_client import InsufficientLiquidityError
        copier.clob.place_order = AsyncMock(
            side_effect=InsufficientLiquidityError("thin book")
        )
        await copier.handle_trade_event(buy_event(market="mkt-y", token="tok-y"))
        assert await copier.portfolio.position_count() == 0
        assert copier.risk.market_exposure("mkt-y") == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_market_reusable_after_failed_order(self, copier):
        # A failed order must not poison the market: a subsequent good order in
        # the same market should still open (exposure was fully released).
        copier.clob.place_order = AsyncMock(side_effect=RuntimeError("boom"))
        await copier.handle_trade_event(buy_event(market="mkt-z", token="tok-z"))
        assert await copier.portfolio.position_count() == 0

        copier.clob.place_order = AsyncMock(return_value={"status": "PAPER"})
        await copier.handle_trade_event(buy_event(market="mkt-z", token="tok-z"))
        assert await copier.portfolio.position_count() == 1


class TestHandlePriceTick:
    @pytest.mark.asyncio
    async def test_take_profit_exit(self, copier):
        from polymarket_copier.core.monitor import PriceTick
        await copier.handle_trade_event(buy_event(price=0.50, token="tok-a"))
        assert await copier.portfolio.position_count() == 1
        # Price jumps to TP (0.70 for entry 0.50) → position closes
        await copier.handle_price_tick(PriceTick(token_id="tok-a", price=0.72))
        assert await copier.portfolio.position_count() == 0

    @pytest.mark.asyncio
    async def test_hold_no_exit(self, copier):
        from polymarket_copier.core.monitor import PriceTick
        await copier.handle_trade_event(buy_event(price=0.50, token="tok-a"))
        await copier.handle_price_tick(PriceTick(token_id="tok-a", price=0.55))
        assert await copier.portfolio.position_count() == 1

    @pytest.mark.asyncio
    async def test_unknown_token_ignored(self, copier):
        from polymarket_copier.core.monitor import PriceTick
        # No position for this token → no error
        await copier.handle_price_tick(PriceTick(token_id="ghost", price=0.55))
        assert await copier.portfolio.position_count() == 0

    @pytest.mark.asyncio
    async def test_multiple_positions_same_token_both_exit(self, copier):
        from polymarket_copier.core.monitor import PriceTick
        # Two tracked traders both buy the SAME token → two separate positions.
        await copier.handle_trade_event(
            buy_event(price=0.50, token="tok-a", wallet="0xwhale")
        )
        await copier.handle_trade_event(
            buy_event(price=0.50, token="tok-a", wallet="0xother")
        )
        assert await copier.portfolio.position_count() == 2

        # A single tick that crosses both stops (SL=0.375 for entry 0.50) must
        # close BOTH positions — the second must not be orphaned.
        await copier.handle_price_tick(PriceTick(token_id="tok-a", price=0.20))
        assert await copier.portfolio.position_count() == 0


class TestStalenessGate:
    @pytest.mark.asyncio
    async def test_stale_trade_skipped(self, copier):
        copier.config.copy_trading.max_trade_age_seconds = 12
        event = buy_event()
        # 60s old → past the 12s budget, alpha decayed.
        stale = TradeEvent(**{**event.__dict__, "timestamp": time.time() - 60})
        await copier.handle_trade_event(stale)
        assert await copier.portfolio.position_count() == 0

    @pytest.mark.asyncio
    async def test_fresh_trade_passes(self, copier):
        copier.config.copy_trading.max_trade_age_seconds = 12
        await copier.handle_trade_event(buy_event())  # timestamp = now
        assert await copier.portfolio.position_count() == 1

    @pytest.mark.asyncio
    async def test_zero_disables_gate(self, copier):
        copier.config.copy_trading.max_trade_age_seconds = 0
        event = buy_event()
        old = TradeEvent(**{**event.__dict__, "timestamp": time.time() - 10_000})
        await copier.handle_trade_event(old)
        assert await copier.portfolio.position_count() == 1


class TestTradingHaltOnEntry:
    @pytest.mark.asyncio
    async def test_entry_blocked_when_halted(self, copier):
        # Daily-loss breaker can no longer be bypassed by opening a new position.
        from unittest.mock import MagicMock
        copier.risk.is_trading_halted = MagicMock(return_value="daily loss limit")
        await copier.handle_trade_event(buy_event())
        assert await copier.portfolio.position_count() == 0


class TestFailClosed:
    @pytest.mark.asyncio
    async def test_skip_when_market_unavailable(self, copier, gamma):
        gamma.get_market = AsyncMock(return_value=None)
        await copier.handle_trade_event(buy_event())
        assert await copier.portfolio.position_count() == 0

    @pytest.mark.asyncio
    async def test_skip_when_price_unavailable(self, copier, gamma):
        gamma.get_market_price = AsyncMock(return_value=None)
        await copier.handle_trade_event(buy_event())
        assert await copier.portfolio.position_count() == 0

    @pytest.mark.asyncio
    async def test_fail_open_when_disabled(self, copier, gamma):
        copier.config.risk_management.fail_closed_on_missing_data = False
        gamma.get_market_price = AsyncMock(return_value=None)
        # With fail-open, falls back to event price and proceeds.
        await copier.handle_trade_event(buy_event())
        assert await copier.portfolio.position_count() == 1


class TestPerTraderAllocationOnCopy:
    @pytest.mark.asyncio
    async def test_trader_cap_blocks_excess_copies(self, copier):
        # Cap a trader at a tiny allocation; a normal copy should breach it.
        copier.risk.cfg.max_trader_allocation = 0.001   # $10 on $10k bankroll
        # Copy size = min(0.5*100, 0.02*10000)= $50 > $10 cap → blocked.
        await copier.handle_trade_event(buy_event(size=100.0))
        assert await copier.portfolio.position_count() == 0
        # And exposure was released (market not poisoned).
        assert copier.risk.trader_exposure("0xwhale") == pytest.approx(0.0)


# ─── Kelly position sizing (opt-in) ───────────────────────────────────────────

async def _seed_closed_trades(portfolio, trader: str, wins: int, losses: int) -> None:
    """Insert N closed winning/losing positions for a trader directly into the DB."""
    db = portfolio._require_db()
    n = 0
    for is_win, count in ((True, wins), (False, losses)):
        for _ in range(count):
            n += 1
            await db.execute(
                """INSERT INTO positions
                   (position_id, market_id, token_id, trader_address, entry_price,
                    tp_price, sl_price, peak_price, size_shares, entry_time,
                    resolve_time, status, exit_price, exit_reason, realized_pnl, closed_at)
                   VALUES (?, 'm', 't', ?, 0.5, 0.7, 0.4, 0.5, 100, 0, NULL,
                           'closed', 0.6, 'TAKE_PROFIT', ?, 0)""",
                (f"seed-{trader}-{n}", trader, 10.0 if is_win else -10.0),
            )
    await db.commit()


class TestKellySizing:
    @pytest.mark.asyncio
    async def test_flat_fallback_when_kelly_disabled(self, copier):
        """kelly_enabled=False → flat size_multiplier formula, even with samples."""
        copier.config.copy_trading.kelly_enabled = False
        await _seed_closed_trades(copier.portfolio, "0xwhale", wins=80, losses=20)
        await copier.handle_trade_event(buy_event(price=0.50, size=100.0, token="tok-a"))
        open_pos = await copier.portfolio.get_open_positions()
        assert len(open_pos) == 1
        # Flat: 0.5*100 = $50 → 100 shares @ 0.50.
        assert open_pos[0].size_shares == pytest.approx(100.0)

    @pytest.mark.asyncio
    async def test_flat_fallback_when_sample_too_small(self, copier):
        """kelly_enabled=True but sample < kelly_min_trades → flat formula."""
        copier.config.copy_trading.kelly_enabled = True
        copier.config.copy_trading.kelly_min_trades = 20
        await _seed_closed_trades(copier.portfolio, "0xwhale", wins=5, losses=0)  # 5 < 20
        await copier.handle_trade_event(buy_event(price=0.50, size=100.0, token="tok-a"))
        open_pos = await copier.portfolio.get_open_positions()
        assert len(open_pos) == 1
        assert open_pos[0].size_shares == pytest.approx(100.0)

    @pytest.mark.asyncio
    async def test_kelly_path_when_enabled_with_sample(self, copier):
        """kelly_enabled=True and enough sample → Kelly sizing (differs from flat)."""
        copier.config.copy_trading.kelly_enabled = True
        copier.config.copy_trading.kelly_min_trades = 20
        copier.config.copy_trading.kelly_fraction_multiplier = 0.25
        copier.config.copy_trading.max_trade_pct = 0.02
        # 70 wins / 100 → win_rate 0.70 at price 0.50.
        await _seed_closed_trades(copier.portfolio, "0xwhale", wins=70, losses=30)
        await copier.handle_trade_event(buy_event(price=0.50, size=100.0, token="tok-a"))
        open_pos = await copier.portfolio.get_open_positions()
        assert len(open_pos) == 1
        # f* = 0.7 - 0.3 = 0.4; raw = 10000*0.4*0.25 = 1000; cap = 2% of 10k = 200.
        # Clamped to $200 → 400 shares @ 0.50. (Flat would be 100 shares.)
        assert open_pos[0].size_shares == pytest.approx(400.0)

    @pytest.mark.asyncio
    async def test_kelly_no_edge_skips_when_enabled(self, copier):
        """Kelly with no edge (win_rate == price) sizes to 0 → no position opened."""
        copier.config.copy_trading.kelly_enabled = True
        copier.config.copy_trading.kelly_min_trades = 20
        # 50 wins / 100 → win_rate 0.50 == price 0.50 → f*=0 → size 0 → no copy.
        await _seed_closed_trades(copier.portfolio, "0xwhale", wins=50, losses=50)
        await copier.handle_trade_event(buy_event(price=0.50, size=100.0, token="tok-a"))
        open_pos = await copier.portfolio.get_open_positions()
        assert len(open_pos) == 0


# ─── Source exit mirroring ────────────────────────────────────────────────────

def sell_event(token="tok-a", wallet="0xwhale") -> TradeEvent:
    return TradeEvent(
        event_id="sell-1", wallet_address=wallet, market_id="mkt-a",
        token_id=token, outcome_label="Yes", trade_type=TradeType.SELL,
        price=0.65, size_usdc=100.0, timestamp=time.time(),
        transaction_hash="0xsell",
    )


class TestSourceExitMirroring:
    @pytest.mark.asyncio
    async def test_source_sell_exits_our_copy_position(self, copier):
        """When the tracked trader sells a token we hold, we must close our copy."""
        copier.config.copy_trading.mirror_source_exits = True
        await copier.handle_trade_event(buy_event(token="tok-a", wallet="0xwhale"))
        assert await copier.portfolio.position_count() == 1

        await copier.handle_trade_event(sell_event(token="tok-a", wallet="0xwhale"))
        assert await copier.portfolio.position_count() == 0

    @pytest.mark.asyncio
    async def test_source_sell_from_different_trader_not_mirrored(self, copier):
        """A sale by a different wallet sharing the same token must NOT close our copy."""
        copier.config.copy_trading.mirror_source_exits = True
        await copier.handle_trade_event(buy_event(token="tok-a", wallet="0xwhale"))
        assert await copier.portfolio.position_count() == 1

        await copier.handle_trade_event(sell_event(token="tok-a", wallet="0xother"))
        assert await copier.portfolio.position_count() == 1

    @pytest.mark.asyncio
    async def test_source_exit_closes_only_selling_traders_copy(self, copier):
        """With two traders holding the same token, a SELL from trader A closes
        only A's copy and leaves trader B's position on that token open."""
        copier.config.copy_trading.mirror_source_exits = True
        await copier.handle_trade_event(buy_event(token="tok-a", wallet="0xwhale"))
        await copier.handle_trade_event(buy_event(token="tok-a", wallet="0xother"))
        assert await copier.portfolio.position_count() == 2

        await copier.handle_trade_event(sell_event(token="tok-a", wallet="0xwhale"))
        remaining = await copier.portfolio.get_open_positions()
        assert len(remaining) == 1
        assert remaining[0].trader_address == "0xother"

    @pytest.mark.asyncio
    async def test_source_sell_with_no_open_position_is_noop(self, copier):
        """A source exit with no matching position must not error or open anything."""
        copier.config.copy_trading.mirror_source_exits = True
        await copier.handle_trade_event(sell_event(token="tok-a", wallet="0xwhale"))
        assert await copier.portfolio.position_count() == 0

    @pytest.mark.asyncio
    async def test_mirror_source_exits_disabled(self, copier):
        """When mirror_source_exits is False, tracked-trader SELLs produce no action."""
        copier.config.copy_trading.mirror_source_exits = False
        await copier.handle_trade_event(buy_event(token="tok-a", wallet="0xwhale"))
        assert await copier.portfolio.position_count() == 1

        await copier.handle_trade_event(sell_event(token="tok-a", wallet="0xwhale"))
        assert await copier.portfolio.position_count() == 1


# ─── Paper fill price propagated to position entry ────────────────────────────

class TestPaperFillPriceInPosition:
    @pytest.mark.asyncio
    async def test_position_entry_price_reflects_fill_slippage(self, copier):
        """Paper BUY fill_price (slippage+fee) should become the position entry_price."""
        await copier.handle_trade_event(buy_event(price=0.50))
        positions = await copier.portfolio.get_open_positions()
        assert len(positions) == 1
        # fill_price = 0.50 * (1 + 0.005 + 0.02) = 0.5125; entry > order price
        assert positions[0].entry_price > 0.50
        assert positions[0].entry_price == pytest.approx(0.5125)

    @pytest.mark.asyncio
    async def test_zero_slippage_keeps_entry_at_current_price(self, copier):
        """With slippage and fee both zero, entry_price equals the order price."""
        copier.config.copy_trading.paper_fill_slippage_pct = 0.0
        copier.config.copy_trading.paper_taker_fee_pct = 0.0
        await copier.handle_trade_event(buy_event(price=0.50))
        positions = await copier.portfolio.get_open_positions()
        assert positions[0].entry_price == pytest.approx(0.50)


# ─── Live fill reconciliation ─────────────────────────────────────────────────

class TestFillReconciliation:
    """After place_order, the opened position and reserved exposure must reflect
    the ACTUAL fill, not an assumed full fill. Paper = full fill (no-op)."""

    @pytest.mark.asyncio
    async def test_paper_full_fill_opens_full_position(self, copier):
        """PAPER reconciliation is a no-op: full position at the paper fill_price."""
        await copier.handle_trade_event(buy_event(price=0.50, size=100.0))
        positions = await copier.portfolio.get_open_positions()
        assert len(positions) == 1
        # 50 USDC / 0.50 = 100 shares, fully filled, unchanged from prior behaviour.
        assert positions[0].size_shares == pytest.approx(100.0)
        # Full registered notional remains reserved (entry $0.50 * 100 = $50).
        assert copier.risk.market_exposure("mkt-a") == pytest.approx(50.0)

    @pytest.mark.asyncio
    async def test_partial_fill_halves_size_and_releases_half_exposure(self, copier):
        """A 50% fill → position size halved and half the registered exposure freed."""
        # Copy size = min(0.5*100, 0.02*10000) = $50 → 100 shares @ 0.50.
        # Registered notional = 0.50 * 100 = $50.
        copier.clob.place_order = AsyncMock(return_value={
            "status": "LIVE", "filled_size": 50.0, "avg_price": 0.50,
        })
        await copier.handle_trade_event(buy_event(price=0.50, size=100.0, market="mkt-p"))
        positions = await copier.portfolio.get_open_positions()
        assert len(positions) == 1
        assert positions[0].size_shares == pytest.approx(50.0)
        # Half the $50 notional released → $25 remains reserved.
        assert copier.risk.market_exposure("mkt-p") == pytest.approx(25.0)
        assert copier.risk.trader_exposure("0xwhale") == pytest.approx(25.0)

    @pytest.mark.asyncio
    async def test_zero_fill_releases_all_exposure_and_opens_no_position(self, copier):
        """A no-fill order opens NO position, subscribes NO token, frees all exposure."""
        from unittest.mock import MagicMock
        copier.monitor = MagicMock()
        copier.clob.place_order = AsyncMock(return_value={
            "status": "LIVE", "filled_size": 0.0, "avg_price": 0.50,
        })
        before = await copier.portfolio.position_count()
        await copier.handle_trade_event(buy_event(price=0.50, size=100.0, market="mkt-0"))
        assert await copier.portfolio.position_count() == before
        # All registered exposure released — market not poisoned.
        assert copier.risk.market_exposure("mkt-0") == pytest.approx(0.0)
        assert copier.risk.trader_exposure("0xwhale") == pytest.approx(0.0)
        # Token must NOT be subscribed for a position that never opened.
        copier.monitor.subscribe_token.assert_not_called()

    @pytest.mark.asyncio
    async def test_matched_amount_field_used_as_fill_size_fallback(self, copier):
        """When filled_size is absent, matched_amount supplies the filled shares."""
        copier.clob.place_order = AsyncMock(return_value={
            "status": "LIVE", "matched_amount": 75.0, "price": 0.50,
        })
        await copier.handle_trade_event(buy_event(price=0.50, size=100.0, market="mkt-m"))
        positions = await copier.portfolio.get_open_positions()
        assert positions[0].size_shares == pytest.approx(75.0)


# ─── Latency logging ─────────────────────────────────────────────────────────

class TestLatencyLogging:
    @pytest.mark.asyncio
    async def test_age_at_detection_logged_for_buy(self, copier, caplog):
        import logging
        with caplog.at_level(logging.INFO, logger="polymarket_copier"):
            await copier.handle_trade_event(buy_event())
        age_logged = any("age=" in r.message for r in caplog.records)
        assert age_logged, "age_at_detection was not logged for BUY event"

    @pytest.mark.asyncio
    async def test_decision_latency_logged_after_successful_copy(self, copier, caplog):
        import logging
        with caplog.at_level(logging.INFO, logger="polymarket_copier"):
            await copier.handle_trade_event(buy_event())
        latency_logged = any("decision_latency" in r.message for r in caplog.records)
        assert latency_logged, "decision_latency was not logged after order placement"

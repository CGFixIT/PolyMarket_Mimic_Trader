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
    g.get_market = AsyncMock(
        return_value=Market(
            condition_id="mkt-a",
            question="Q?",
            volume_24h=50_000,
            active=True,
            resolve_time=None,
        )
    )
    g.get_market_price = AsyncMock(return_value=0.50)
    return g


@pytest.fixture
def copier(config, portfolio, gamma):
    risk = RiskManager(config=RiskConfig(), bankroll=config.bankroll)
    clob = ClobClient(config)
    return CopyTrader(risk, portfolio, clob, gamma, config)


def buy_event(price=0.50, size=100.0, market="mkt-a", token="tok-a", wallet="0xwhale") -> TradeEvent:
    return TradeEvent(
        event_id="e1",
        wallet_address=wallet,
        market_id=market,
        token_id=token,
        outcome_label="Yes",
        trade_type=TradeType.BUY,
        price=price,
        size_usdc=size,
        timestamp=time.time(),
        transaction_hash="0xhash",
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
        gamma.get_market = AsyncMock(
            return_value=Market(
                condition_id="mkt-a",
                volume_24h=100,
                active=True,
                resolve_time=None,
            )
        )
        await copier.handle_trade_event(buy_event())
        assert await copier.portfolio.position_count() == 0

    @pytest.mark.asyncio
    async def test_resolution_blackout_skip(self, copier, gamma):
        from datetime import datetime, timezone, timedelta

        soon = datetime.now(timezone.utc) + timedelta(hours=6)
        gamma.get_market = AsyncMock(
            return_value=Market(
                condition_id="mkt-a",
                volume_24h=50_000,
                active=True,
                resolve_time=soon,
            )
        )
        await copier.handle_trade_event(buy_event())
        assert await copier.portfolio.position_count() == 0

    @pytest.mark.asyncio
    async def test_max_concurrent_positions(self, copier):
        copier.config.copy_trading.max_concurrent_positions = 1
        await copier.handle_trade_event(buy_event(market="mkt-a", token="tok-a"))
        await copier.handle_trade_event(buy_event(market="mkt-b", token="tok-b"))
        assert await copier.portfolio.position_count() == 1


class TestMaxPositionsPerToken:
    """M9: cap concurrent open positions on a single token. Two tracked traders
    buying the same token must not pile unbounded copies onto one outcome."""

    @pytest.mark.asyncio
    async def test_per_token_cap_blocks_excess_copies(self, copier):
        copier.config.copy_trading.max_positions_per_token = 2
        # Three different traders all buy the SAME token. Only 2 should open.
        await copier.handle_trade_event(buy_event(token="tok-a", wallet="0xwhale1"))
        await copier.handle_trade_event(buy_event(token="tok-a", wallet="0xwhale2"))
        await copier.handle_trade_event(buy_event(token="tok-a", wallet="0xwhale3"))
        positions = await copier.portfolio.get_positions_by_token("tok-a")
        assert len(positions) == 2

    @pytest.mark.asyncio
    async def test_per_token_cap_independent_across_tokens(self, copier):
        # A full token does not block copies on a DIFFERENT token.
        copier.config.copy_trading.max_positions_per_token = 1
        await copier.handle_trade_event(buy_event(token="tok-a", wallet="0xwhale1"))
        await copier.handle_trade_event(buy_event(token="tok-a", wallet="0xwhale2"))
        await copier.handle_trade_event(buy_event(token="tok-b", wallet="0xwhale3"))
        assert len(await copier.portfolio.get_positions_by_token("tok-a")) == 1
        assert len(await copier.portfolio.get_positions_by_token("tok-b")) == 1

    @pytest.mark.asyncio
    async def test_per_token_cap_zero_disables(self, copier):
        # 0 disables the per-token cap entirely (only global cap applies).
        copier.config.copy_trading.max_positions_per_token = 0
        await copier.handle_trade_event(buy_event(token="tok-a", wallet="0xwhale1"))
        await copier.handle_trade_event(buy_event(token="tok-a", wallet="0xwhale2"))
        await copier.handle_trade_event(buy_event(token="tok-a", wallet="0xwhale3"))
        assert len(await copier.portfolio.get_positions_by_token("tok-a")) == 3


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

        copier.clob.place_order = AsyncMock(side_effect=InsufficientLiquidityError("thin book"))
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


class TestEdgeRevalidation:
    """M1: re-fetch the price after acquiring the entry lock and skip if it moved
    adversely beyond max_price_deviation since detection. Guards against entering on
    a stale edge after waiting behind another concurrent entry."""

    @pytest.mark.asyncio
    async def test_edge_collapse_skips_entry(self, copier, gamma):
        # Detection price 0.50 (passes H6 gate vs event 0.50), but by the time we
        # hold the lock the price jumped to 0.60 (+20% adverse) → skip.
        gamma.get_market_price = AsyncMock(side_effect=[0.50, 0.60])
        await copier.handle_trade_event(buy_event(price=0.50))
        assert await copier.portfolio.position_count() == 0
        # No phantom exposure left behind (skip ran before build_position).
        assert copier.risk.market_exposure("mkt-a") == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_favorable_move_still_enters(self, copier, gamma):
        # Price dropped 0.50 → 0.45 between detection and order: favorable, more
        # upside to the same TP. Revalidation must NOT skip a favorable move.
        gamma.get_market_price = AsyncMock(side_effect=[0.50, 0.45])
        await copier.handle_trade_event(buy_event(price=0.50))
        assert await copier.portfolio.position_count() == 1

    @pytest.mark.asyncio
    async def test_small_adverse_move_within_tolerance_enters(self, copier, gamma):
        # +1% move (0.50 → 0.505) is within max_price_deviation (2%) → still enters.
        gamma.get_market_price = AsyncMock(side_effect=[0.50, 0.505])
        await copier.handle_trade_event(buy_event(price=0.50))
        assert await copier.portfolio.position_count() == 1

    @pytest.mark.asyncio
    async def test_revalidation_disabled_skips_second_fetch(self, copier, gamma):
        # With revalidation OFF, the adverse 0.60 second value is never consumed —
        # the position opens at the detection price (proves no second fetch ran).
        copier.config.copy_trading.revalidate_edge_before_order = False
        gamma.get_market_price = AsyncMock(side_effect=[0.50, 0.60])
        await copier.handle_trade_event(buy_event(price=0.50))
        assert await copier.portfolio.position_count() == 1

    @pytest.mark.asyncio
    async def test_revalidation_price_unavailable_fails_closed(self, copier, gamma):
        # Second fetch returns None → fail-closed skip (no trading on missing data).
        gamma.get_market_price = AsyncMock(side_effect=[0.50, None])
        await copier.handle_trade_event(buy_event(price=0.50))
        assert await copier.portfolio.position_count() == 0


class TestOrderTypeSelection:
    """M5: entries use FOK (all-or-nothing immediate), exits use FAK (take available
    liquidity now). Both are configurable; verify the correct type reaches the CLOB."""

    @pytest.mark.asyncio
    async def test_entry_places_fok_order(self, copier):
        captured = []
        orig = copier.clob.place_order

        async def spy(order):
            captured.append(order)
            return await orig(order)

        copier.clob.place_order = spy
        await copier.handle_trade_event(buy_event())
        buys = [o for o in captured if o.side == "BUY"]
        assert buys, "no entry order placed"
        assert buys[0].order_type == "FOK"

    @pytest.mark.asyncio
    async def test_exit_places_fak_order(self, copier):
        from polymarket_copier.core.monitor import PriceTick

        await copier.handle_trade_event(buy_event(price=0.50, token="tok-a"))
        captured = []
        orig = copier.clob.place_order

        async def spy(order):
            captured.append(order)
            return await orig(order)

        copier.clob.place_order = spy
        # Price jumps to TP (0.70 for entry 0.50) → exit fires.
        await copier.handle_price_tick(PriceTick(token_id="tok-a", price=0.72))
        sells = [o for o in captured if o.side == "SELL"]
        assert sells, "no exit order placed"
        assert sells[0].order_type == "FAK"
        assert await copier.portfolio.position_count() == 0

    @pytest.mark.asyncio
    async def test_entry_order_type_is_configurable(self, copier):
        copier.config.copy_trading.entry_order_type = "GTC"
        captured = []
        orig = copier.clob.place_order

        async def spy(order):
            captured.append(order)
            return await orig(order)

        copier.clob.place_order = spy
        await copier.handle_trade_event(buy_event())
        buys = [o for o in captured if o.side == "BUY"]
        assert buys and buys[0].order_type == "GTC"


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
        await copier.handle_trade_event(buy_event(price=0.50, token="tok-a", wallet="0xwhale"))
        await copier.handle_trade_event(buy_event(price=0.50, token="tok-a", wallet="0xother"))
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
        copier.risk.cfg.max_trader_allocation = 0.001  # $10 on $10k bankroll
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
        event_id="sell-1",
        wallet_address=wallet,
        market_id="mkt-a",
        token_id=token,
        outcome_label="Yes",
        trade_type=TradeType.SELL,
        price=0.65,
        size_usdc=100.0,
        timestamp=time.time(),
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


class TestSourceExitConcurrency:
    """M17: source-exits for a token run concurrently with isolated failures, so a
    single failing/slow exit neither cancels its siblings nor parks the poll path."""

    @pytest.mark.asyncio
    async def test_one_failing_exit_does_not_block_others(self, copier):
        copier.config.copy_trading.mirror_source_exits = True
        copier.config.copy_trading.max_positions_per_token = 5
        # Two copies of the same token from the same tracked wallet.
        await copier.handle_trade_event(buy_event(token="tok-a", wallet="0xwhale"))
        await copier.handle_trade_event(buy_event(token="tok-a", wallet="0xwhale"))
        assert await copier.portfolio.position_count() == 2

        attempted = []
        orig_exit = copier._exit_position
        calls = {"n": 0}

        async def flaky_exit(pos, price, reason):
            attempted.append(pos.position_id)
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated venue error on first exit")
            return await orig_exit(pos, price, reason)

        copier._exit_position = flaky_exit
        # Must not raise despite one exit failing (gather return_exceptions).
        await copier.handle_trade_event(sell_event(token="tok-a", wallet="0xwhale"))
        # Both positions were attempted; the non-failing one actually closed.
        assert len(attempted) == 2
        assert await copier.portfolio.position_count() == 1


class TestConvictionSizing:
    """M4: copy size is tilted by the whale's trade size RELATIVE to their own
    typical (median) bet — bounded to [min, max] conviction mult — instead of
    treating absolute USDC as the conviction proxy."""

    async def _shares(self, copier):
        positions = await copier.portfolio.get_open_positions()
        assert len(positions) == 1
        return positions[0].size_shares

    @pytest.mark.asyncio
    async def test_disabled_leaves_size_unchanged(self, copier):
        # Baseline: size_multiplier 0.5 → 50 USDC / 0.50 = 100 shares.
        copier.config.copy_trading.conviction_sizing_enabled = False
        copier.update_tracker_typical_sizes({"0xwhale": 10.0})
        await copier.handle_trade_event(buy_event(price=0.50, size=100.0, wallet="0xwhale"))
        assert await self._shares(copier) == pytest.approx(100.0)

    @pytest.mark.asyncio
    async def test_high_conviction_sizes_up_capped(self, copier):
        # typical=10, size=100 → raw mult 10 → capped at max_conviction_mult (2.0).
        # base 50 USDC * 2.0 = 100 USDC / 0.50 = 200 shares (capped, NOT 10x).
        copier.config.copy_trading.conviction_sizing_enabled = True
        copier.config.copy_trading.max_conviction_mult = 2.0
        copier.update_tracker_typical_sizes({"0xwhale": 10.0})
        await copier.handle_trade_event(buy_event(price=0.50, size=100.0, wallet="0xwhale"))
        assert await self._shares(copier) == pytest.approx(200.0)

    @pytest.mark.asyncio
    async def test_low_conviction_sizes_down_floored(self, copier):
        # typical=1000, size=100 → raw mult 0.1 → floored at min_conviction_mult (0.5).
        # base 50 USDC * 0.5 = 25 USDC / 0.50 = 50 shares.
        copier.config.copy_trading.conviction_sizing_enabled = True
        copier.config.copy_trading.min_conviction_mult = 0.5
        copier.update_tracker_typical_sizes({"0xwhale": 1000.0})
        await copier.handle_trade_event(buy_event(price=0.50, size=100.0, wallet="0xwhale"))
        assert await self._shares(copier) == pytest.approx(50.0)

    @pytest.mark.asyncio
    async def test_unknown_typical_is_noop(self, copier):
        # No typical-size entry for this wallet → tilt is skipped, size unchanged.
        copier.config.copy_trading.conviction_sizing_enabled = True
        copier.update_tracker_typical_sizes({"0xother": 10.0})
        await copier.handle_trade_event(buy_event(price=0.50, size=100.0, wallet="0xwhale"))
        assert await self._shares(copier) == pytest.approx(100.0)

    @pytest.mark.asyncio
    async def test_tilt_never_breaches_per_trade_cap(self, copier):
        # Even a maxed conviction tilt is clamped by max_trade_pct (2% of 10k = 200).
        # base from a large source trade is already capped at 200 USDC; *2.0 then
        # re-clamped to 200 USDC / 0.50 = 400 shares (the hard ceiling holds).
        copier.config.copy_trading.conviction_sizing_enabled = True
        copier.config.copy_trading.max_conviction_mult = 2.0
        copier.update_tracker_typical_sizes({"0xwhale": 10.0})
        await copier.handle_trade_event(buy_event(price=0.50, size=100_000.0, wallet="0xwhale"))
        assert await self._shares(copier) == pytest.approx(400.0)


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
        copier.clob.place_order = AsyncMock(
            return_value={
                "status": "LIVE",
                "filled_size": 50.0,
                "avg_price": 0.50,
            }
        )
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
        copier.clob.place_order = AsyncMock(
            return_value={
                "status": "LIVE",
                "filled_size": 0.0,
                "avg_price": 0.50,
            }
        )
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
        copier.clob.place_order = AsyncMock(
            return_value={
                "status": "LIVE",
                "matched_amount": 75.0,
                "price": 0.50,
            }
        )
        await copier.handle_trade_event(buy_event(price=0.50, size=100.0, market="mkt-m"))
        positions = await copier.portfolio.get_open_positions()
        assert positions[0].size_shares == pytest.approx(75.0)


async def _closed_record(copier, position_id):
    """Read (exit_price, exit_reason, realized_pnl) for a closed position row."""
    db = copier.portfolio._require_db()
    cursor = await db.execute(
        "SELECT exit_price, exit_reason, realized_pnl FROM positions WHERE position_id=?",
        (position_id,),
    )
    row = await cursor.fetchone()
    return row[0], row[1], row[2]


class TestResolutionSettlement:
    """M14: a near-resolution SELL that can't fill is booked at its 0/1 outcome
    and the shares redeemed on-chain, instead of leaving stuck un-sellable dust."""

    async def _open_position(self, copier, *, entry=0.50, shares=100.0, resolve_in_hours=1.0):
        """Open a position directly (bypassing the entry blackout gate) with a
        resolve_time `resolve_in_hours` from now, and return it."""
        resolve_ts = time.time() + resolve_in_hours * 3600.0
        pos = await copier.risk.build_position(
            position_id="pos-settle",
            market_id="mkt-s",
            token_id="tok-s",
            trader_address="0xwhale",
            entry_price=entry,
            size_shares=shares,
            resolve_time=resolve_ts,
        )
        await copier.portfolio.open_position(pos)
        return pos

    @pytest.mark.asyncio
    async def test_winning_side_settles_at_one(self, copier):
        from polymarket_copier.core.risk_manager import ExitReason

        pos = await self._open_position(copier, entry=0.50, shares=100.0, resolve_in_hours=1.0)
        copier.clob.place_order_with_timeout = AsyncMock(return_value={"status": "LIVE", "filled_size": 0.0})
        copier.clob.redeem_position = AsyncMock(return_value=True)

        # Price extreme high (>= 0.90 threshold) → held token wins → settle at 1.0.
        await copier._exit_position_locked(pos, 0.95, ExitReason.MARKET_RESOLVING)

        assert await copier.portfolio.position_count() == 0
        copier.clob.redeem_position.assert_awaited_once()
        exit_price, exit_reason, pnl = await _closed_record(copier, "pos-settle")
        assert exit_price == pytest.approx(1.0)
        assert exit_reason == "MARKET_SETTLED"
        # PnL = (1.0 - 0.50) * 100 = +50.
        assert pnl == pytest.approx(50.0)

    @pytest.mark.asyncio
    async def test_losing_side_settles_at_zero(self, copier):
        from polymarket_copier.core.risk_manager import ExitReason

        pos = await self._open_position(copier, entry=0.50, shares=100.0, resolve_in_hours=1.0)
        copier.clob.place_order_with_timeout = AsyncMock(return_value={"status": "LIVE", "filled_size": 0.0})
        copier.clob.redeem_position = AsyncMock(return_value=True)

        # Price extreme low (<= 0.10) → held token loses → settle at 0.0.
        await copier._exit_position_locked(pos, 0.05, ExitReason.MARKET_RESOLVING)

        assert await copier.portfolio.position_count() == 0
        exit_price, exit_reason, pnl = await _closed_record(copier, "pos-settle")
        assert exit_price == pytest.approx(0.0)
        assert exit_reason == "MARKET_SETTLED"
        # PnL = (0.0 - 0.50) * 100 = -50.
        assert pnl == pytest.approx(-50.0)

    @pytest.mark.asyncio
    async def test_ambiguous_midprice_leaves_position_open(self, copier):
        from polymarket_copier.core.risk_manager import ExitReason

        pos = await self._open_position(copier, resolve_in_hours=1.0)
        copier.clob.place_order_with_timeout = AsyncMock(return_value={"status": "LIVE", "filled_size": 0.0})
        copier.clob.redeem_position = AsyncMock(return_value=True)

        # 0.50 is not extreme enough to call the outcome → no settlement.
        await copier._exit_position_locked(pos, 0.50, ExitReason.MARKET_RESOLVING)

        assert await copier.portfolio.position_count() == 1  # still open
        copier.clob.redeem_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_not_near_resolution_leaves_position_open(self, copier):
        from polymarket_copier.core.risk_manager import ExitReason

        # 100h out — well outside the 24h blackout. A failed fill is just thin
        # liquidity, not a resolution lockout → don't fabricate a settlement.
        pos = await self._open_position(copier, resolve_in_hours=100.0)
        copier.clob.place_order_with_timeout = AsyncMock(return_value={"status": "LIVE", "filled_size": 0.0})
        copier.clob.redeem_position = AsyncMock(return_value=True)

        await copier._exit_position_locked(pos, 0.95, ExitReason.MARKET_RESOLVING)

        assert await copier.portfolio.position_count() == 1
        copier.clob.redeem_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_settlement_disabled_leaves_position_open(self, copier):
        from polymarket_copier.core.risk_manager import ExitReason

        copier.config.risk_management.settlement_enabled = False
        pos = await self._open_position(copier, resolve_in_hours=1.0)
        copier.clob.place_order_with_timeout = AsyncMock(return_value={"status": "LIVE", "filled_size": 0.0})
        copier.clob.redeem_position = AsyncMock(return_value=True)

        await copier._exit_position_locked(pos, 0.95, ExitReason.MARKET_RESOLVING)

        assert await copier.portfolio.position_count() == 1
        copier.clob.redeem_position.assert_not_called()

    def test_settlement_value_thresholds(self, copier):
        from types import SimpleNamespace

        near = SimpleNamespace(resolve_time=time.time() + 3600.0)  # 1h out, in blackout
        assert copier._settlement_value(near, 0.90) == 1.0  # at threshold
        assert copier._settlement_value(near, 0.99) == 1.0
        assert copier._settlement_value(near, 0.09) == 0.0  # below 1 - threshold
        assert copier._settlement_value(near, 0.01) == 0.0
        assert copier._settlement_value(near, 0.50) is None  # ambiguous
        # No resolve_time → never settles.
        assert copier._settlement_value(SimpleNamespace(resolve_time=None), 0.95) is None


# ─── Kelly tracker-prior seeding ─────────────────────────────────────────────


class TestKellyTrackerPrior:
    """H18: Kelly sizing uses the tracker's DEMONSTRATED edge (from mean per-trade
    ROI) as a warm-up prior when the bot's own closed-trade sample is too small."""

    @pytest.mark.asyncio
    async def test_tracker_prior_used_when_sample_too_small(self, copier):
        """With sample < min_trades and a tracker ROI, Kelly sizes from the edge."""
        copier.config.copy_trading.kelly_enabled = True
        copier.config.copy_trading.kelly_min_trades = 20
        copier.config.copy_trading.kelly_seed_from_tracker = True
        copier.config.copy_trading.kelly_fraction_multiplier = 0.25
        copier.config.copy_trading.max_trade_pct = 0.02
        copier.config.copy_trading.kelly_edge_shrink = 0.5
        copier.config.copy_trading.kelly_max_edge = 0.20
        # 5 closed trades (<20 min), but tracker shows a strong +40% mean ROI.
        await _seed_closed_trades(copier.portfolio, "0xwhale", wins=5, losses=0)
        copier.update_tracker_mean_pnl({"0xwhale": 0.40})

        await copier.handle_trade_event(buy_event(price=0.50, size=100.0, token="tok-a"))
        open_pos = await copier.portfolio.get_open_positions()
        assert len(open_pos) == 1
        # edge = roi_to_edge(0.40, 0.50) = 0.20; decay≈1 (just updated).
        # edge_to_win_prob: 0.20*0.5=0.10 (≤max_edge) → p = 0.50+0.10 = 0.60.
        # f* = 0.60 - 0.40*0.50/0.50 = 0.20; raw = 10k*0.20*0.25 = $500, cap $200 → 400 shares.
        # Flat sizing would give 100 shares — confirms the edge path was used.
        assert open_pos[0].size_shares == pytest.approx(400.0)

    @pytest.mark.asyncio
    async def test_flat_fallback_when_seeding_disabled(self, copier):
        """kelly_seed_from_tracker=False → flat formula even when a tracker ROI exists."""
        copier.config.copy_trading.kelly_enabled = True
        copier.config.copy_trading.kelly_min_trades = 20
        copier.config.copy_trading.kelly_seed_from_tracker = False
        copier.update_tracker_mean_pnl({"0xwhale": 0.40})

        await copier.handle_trade_event(buy_event(price=0.50, size=100.0, token="tok-a"))
        open_pos = await copier.portfolio.get_open_positions()
        assert len(open_pos) == 1
        # Flat: 0.5 * $100 = $50 → 100 shares @ 0.50.
        assert open_pos[0].size_shares == pytest.approx(100.0)

    @pytest.mark.asyncio
    async def test_flat_fallback_when_wallet_not_in_tracker(self, copier):
        """Seeding enabled but no tracker ROI for this wallet → flat formula."""
        copier.config.copy_trading.kelly_enabled = True
        copier.config.copy_trading.kelly_min_trades = 20
        copier.config.copy_trading.kelly_seed_from_tracker = True
        # Tracker ROI for a *different* wallet only.
        copier.update_tracker_mean_pnl({"0xother": 0.40})

        await copier.handle_trade_event(buy_event(price=0.50, size=100.0, token="tok-a"))
        open_pos = await copier.portfolio.get_open_positions()
        assert len(open_pos) == 1
        assert open_pos[0].size_shares == pytest.approx(100.0)

    @pytest.mark.asyncio
    async def test_favorite_buyer_not_oversized(self, copier):
        """H18 core: a trader with zero ROI (favorite-buyer) has ~0 edge → p ≈ price →
        no Kelly edge → size 0 → no position. The OLD win-rate bug would oversize here."""
        copier.config.copy_trading.kelly_enabled = True
        copier.config.copy_trading.kelly_min_trades = 20
        copier.config.copy_trading.kelly_seed_from_tracker = True
        await _seed_closed_trades(copier.portfolio, "0xwhale", wins=5, losses=0)
        copier.update_tracker_mean_pnl({"0xwhale": 0.0})  # no demonstrated edge

        await copier.handle_trade_event(buy_event(price=0.50, size=100.0, token="tok-a"))
        # Kelly enabled + zero edge → size 0 → skipped (no position opened).
        assert await copier.portfolio.position_count() == 0

    @pytest.mark.asyncio
    async def test_stale_tracker_prior_decays_size(self, copier):
        """M4: an aged tracker prior decays the edge toward zero → far smaller size
        than the same prior fresh."""
        copier.config.copy_trading.kelly_enabled = True
        copier.config.copy_trading.kelly_min_trades = 20
        copier.config.copy_trading.kelly_seed_from_tracker = True
        copier.config.copy_trading.tracker_prior_decay_enabled = True
        copier.config.copy_trading.kelly_fraction_multiplier = 0.25
        copier.config.copy_trading.max_trade_pct = 0.02
        copier.config.copy_trading.order_min_shares = 0  # isolate Kelly decay; min-size filter tested separately
        await _seed_closed_trades(copier.portfolio, "0xwhale", wins=5, losses=0)
        copier.update_tracker_mean_pnl({"0xwhale": 0.40})
        # Backdate the update ~1000h → decay = 1/(1+1000) ≈ 0.001 → edge ≈ 0.
        copier._tracker_updated_at = time.time() - 1000 * 3600

        await copier.handle_trade_event(buy_event(price=0.50, size=100.0, token="tok-a"))
        open_pos = await copier.portfolio.get_open_positions()
        # Fresh, this prior caps at 400 shares (see test above); decayed it is a
        # tiny fraction of that — proving the prior was down-weighted by age.
        assert len(open_pos) == 1
        assert open_pos[0].size_shares < 10.0

    @pytest.mark.asyncio
    async def test_decay_disabled_keeps_full_prior(self, copier):
        """M4 kill-switch: tracker_prior_decay_enabled=False → no age down-weight,
        so even a stale prior sizes at full strength (caps at 400 shares here)."""
        copier.config.copy_trading.kelly_enabled = True
        copier.config.copy_trading.kelly_min_trades = 20
        copier.config.copy_trading.kelly_seed_from_tracker = True
        copier.config.copy_trading.tracker_prior_decay_enabled = False
        copier.config.copy_trading.kelly_fraction_multiplier = 0.25
        copier.config.copy_trading.max_trade_pct = 0.02
        await _seed_closed_trades(copier.portfolio, "0xwhale", wins=5, losses=0)
        copier.update_tracker_mean_pnl({"0xwhale": 0.40})
        copier._tracker_updated_at = time.time() - 1000 * 3600  # stale, but decay off

        await copier.handle_trade_event(buy_event(price=0.50, size=100.0, token="tok-a"))
        open_pos = await copier.portfolio.get_open_positions()
        assert len(open_pos) == 1
        assert open_pos[0].size_shares == pytest.approx(400.0)

    @pytest.mark.asyncio
    async def test_own_sample_takes_over_once_sufficient(self, copier):
        """When bot's own sample >= min_trades, own rate wins over tracker prior."""
        copier.config.copy_trading.kelly_enabled = True
        copier.config.copy_trading.kelly_min_trades = 20
        copier.config.copy_trading.kelly_seed_from_tracker = True
        copier.config.copy_trading.kelly_fraction_multiplier = 0.25
        copier.config.copy_trading.max_trade_pct = 0.02
        # Bot's own: 50 wins / 50 losses → win_rate=0.50=price → no edge → size=0.
        # Tracker rate=0.70 → Kelly $200 (400 shares), but own sample wins.
        await _seed_closed_trades(copier.portfolio, "0xwhale", wins=50, losses=50)
        copier.update_tracker_win_rates({"0xwhale": 0.70})

        await copier.handle_trade_event(buy_event(price=0.50, size=100.0, token="tok-a"))
        # Own win_rate=0.50 == price → f*=0 → no position opened.
        assert await copier.portfolio.position_count() == 0

    def test_update_tracker_win_rates_replaces_prior_mapping(self, copier):
        """update_tracker_win_rates() fully replaces the previous dict (not merges)."""
        copier.update_tracker_win_rates({"0xold": 0.60})
        copier.update_tracker_win_rates({"0xnew": 0.75})
        assert "0xold" not in copier._tracker_win_rates
        assert copier._tracker_win_rates == {"0xnew": 0.75}


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


# ─── Entry-path TOCTOU lock ───────────────────────────────────────────────────


class TestEntryTocTouLock:
    """Concurrent wallet polls must not both pass the position-count gate and
    both open positions when max_concurrent_positions == 1."""

    @pytest.mark.asyncio
    async def test_concurrent_buys_respect_max_positions(self, copier):
        """Two simultaneous handle_trade_event calls for different markets must
        not both open when max_concurrent_positions == 1."""
        import asyncio

        copier.config.copy_trading.max_concurrent_positions = 1

        event_a = buy_event(market="mkt-a", token="tok-a", wallet="0xwhale")
        event_b = buy_event(market="mkt-b", token="tok-b", wallet="0xother")

        # Run both concurrently — the _entry_lock ensures only one wins.
        await asyncio.gather(
            copier.handle_trade_event(event_a),
            copier.handle_trade_event(event_b),
        )

        count = await copier.portfolio.position_count()
        assert count == 1, f"Expected exactly 1 position, got {count} (TOCTOU race)"

    @pytest.mark.asyncio
    async def test_serial_buys_still_open_up_to_cap(self, copier):
        """Serial calls (no concurrency) should still accumulate up to the cap."""
        copier.config.copy_trading.max_concurrent_positions = 2
        await copier.handle_trade_event(buy_event(market="mkt-a", token="tok-a"))
        await copier.handle_trade_event(buy_event(market="mkt-b", token="tok-b"))
        await copier.handle_trade_event(buy_event(market="mkt-c", token="tok-c"))
        count = await copier.portfolio.position_count()
        assert count == 2, "Should be capped at 2 even with serial calls"


class TestStructuredEvents:
    """M16/M17: lifecycle events emit machine-readable structured records through
    the logging 'data' channel, and the skip counter is exercised on every skip."""

    @staticmethod
    def _capture_events():
        """Attach a capturing handler to the copier logger and lower its level so
        INFO-level structured events are not filtered (setup_logger is not called in
        tests, so the logger's effective level would otherwise be WARNING). Returns
        (records, cleanup) where cleanup() detaches the handler and restores level."""
        import logging

        records = []

        class _Cap(logging.Handler):
            def emit(self, record):
                if hasattr(record, "data"):
                    records.append(record.data)

        lg = logging.getLogger("polymarket_copier")
        prev_level = lg.level
        lg.setLevel(logging.DEBUG)
        handler = _Cap()
        lg.addHandler(handler)

        def cleanup():
            lg.removeHandler(handler)
            lg.setLevel(prev_level)

        return records, cleanup

    @pytest.mark.asyncio
    async def test_position_opened_event_emitted(self, copier):
        records, cleanup = self._capture_events()
        try:
            await copier.handle_trade_event(buy_event(price=0.50, token="tok-a"))
        finally:
            cleanup()
        opened = [r for r in records if r.get("event") == "position_opened"]
        assert len(opened) == 1
        ev = opened[0]
        assert ev["side"] == "BUY"
        assert ev["token_id"] == "tok-a"
        assert ev["trader"] == "0xwhale"
        assert ev["mode"] == "paper"
        assert "tp_price" in ev and "sl_price" in ev

    @pytest.mark.asyncio
    async def test_copy_skipped_event_carries_reason(self, copier, gamma):
        # Force a low-volume skip and assert the structured copy_skipped event fires.
        gamma.get_market = AsyncMock(
            return_value=Market(
                condition_id="mkt-a",
                volume_24h=100,
                active=True,
                resolve_time=None,
            )
        )
        records, cleanup = self._capture_events()
        try:
            await copier.handle_trade_event(buy_event())
        finally:
            cleanup()
        skips = [r for r in records if r.get("event") == "copy_skipped"]
        assert skips and skips[0]["reason"] == "low_volume"
        assert skips[0]["trader"] == "0xwhale"

    @pytest.mark.asyncio
    async def test_position_closed_event_emitted_on_exit(self, copier):
        from polymarket_copier.core.monitor import PriceTick

        await copier.handle_trade_event(buy_event(price=0.50, token="tok-a"))
        records, cleanup = self._capture_events()
        try:
            # Jump to TP (0.70 for entry 0.50) → exit fires.
            await copier.handle_price_tick(PriceTick(token_id="tok-a", price=0.72))
        finally:
            cleanup()
        closed = [r for r in records if r.get("event") == "position_closed"]
        assert len(closed) == 1
        assert closed[0]["reason"] == "TAKE_PROFIT"
        assert "pnl" in closed[0]

    @pytest.mark.asyncio
    async def test_circuit_breaker_event_emitted_when_halted(self, copier):
        # Drive daily PnL below the loss limit so the entry path halts.
        from decimal import Decimal

        copier.risk._daily_pnl = Decimal(str(-(copier.risk.bankroll * copier.risk.cfg.daily_loss_limit_pct) - 1.0))
        records, cleanup = self._capture_events()
        try:
            await copier.handle_trade_event(buy_event())
        finally:
            cleanup()
        cb = [r for r in records if r.get("event") == "circuit_breaker_tripped"]
        assert cb, "expected a circuit_breaker_tripped event"
        assert await copier.portfolio.position_count() == 0


class TestSkipReasons:
    """M16/M17: each early-return abandons the copy with a stable skip reason.
    Capturing the structured copy_skipped events validates the reason codes and
    covers the instrumented skip branches."""

    @staticmethod
    def _skips(records):
        return [r for r in records if r.get("event") == "copy_skipped"]

    @staticmethod
    def _capture():
        import logging

        records = []

        class _Cap(logging.Handler):
            def emit(self, record):
                if hasattr(record, "data"):
                    records.append(record.data)

        lg = logging.getLogger("polymarket_copier")
        prev = lg.level
        lg.setLevel(logging.DEBUG)
        h = _Cap()
        lg.addHandler(h)
        return records, lambda: (lg.removeHandler(h), lg.setLevel(prev))

    @pytest.mark.asyncio
    async def test_adverse_price_move_reason(self, copier, gamma):
        gamma.get_market_price = AsyncMock(return_value=0.60)  # +20% vs whale 0.50
        records, cleanup = self._capture()
        try:
            await copier.handle_trade_event(buy_event(price=0.50))
        finally:
            cleanup()
        assert any(s["reason"] == "adverse_price_move" for s in self._skips(records))

    @pytest.mark.asyncio
    async def test_entry_price_band_reason(self, copier, gamma):
        # Price at 0.99 is outside the default [0.05, 0.95] band; keep it within
        # max_price_deviation of the whale entry so the band gate is what fires.
        gamma.get_market_price = AsyncMock(return_value=0.99)
        records, cleanup = self._capture()
        try:
            await copier.handle_trade_event(buy_event(price=0.99))
        finally:
            cleanup()
        assert any(s["reason"] == "entry_price_band" for s in self._skips(records))

    @pytest.mark.asyncio
    async def test_stale_trade_reason(self, copier):
        copier.config.copy_trading.max_trade_age_seconds = 5.0
        stale = buy_event()
        object.__setattr__(stale, "timestamp", time.time() - 60)  # 60s old > 5s max
        records, cleanup = self._capture()
        try:
            await copier.handle_trade_event(stale)
        finally:
            cleanup()
        assert any(s["reason"] == "stale_trade" for s in self._skips(records))

    @pytest.mark.asyncio
    async def test_exposure_cap_reason(self, copier):
        # Tiny per-market cap so the first copy's reservation breaches it.
        copier.risk.cfg.max_market_exposure_pct = 0.0001  # ~$1 cap on $10k bankroll
        records, cleanup = self._capture()
        try:
            await copier.handle_trade_event(buy_event(price=0.50, size=100.0))
        finally:
            cleanup()
        assert any(s["reason"] == "exposure_cap" for s in self._skips(records))

    @pytest.mark.asyncio
    async def test_missing_market_data_reason(self, copier, gamma):
        gamma.get_market = AsyncMock(return_value=None)
        records, cleanup = self._capture()
        try:
            await copier.handle_trade_event(buy_event())
        finally:
            cleanup()
        assert any(s["reason"] == "missing_market_data" for s in self._skips(records))

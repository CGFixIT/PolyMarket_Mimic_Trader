"""Tests for the v2 SQLite-backed portfolio manager."""

from __future__ import annotations

import pytest

from polymarket_copier.core.portfolio import PortfolioManager
from polymarket_copier.core.risk_manager import ExitReason, RiskConfig, RiskManager


@pytest.fixture
def rm() -> RiskManager:
    # max_trader_allocation=1.0 keeps the per-trader cap out of these tests, which
    # exercise portfolio persistence, not the allocation control (tested elsewhere).
    return RiskManager(
        config=RiskConfig(max_trader_allocation=1.0), bankroll=10_000.0
    )


@pytest.fixture
async def portfolio(tmp_path):
    pm = PortfolioManager(db_path=str(tmp_path / "test_positions.db"))
    await pm.init()
    yield pm
    await pm.close()


async def make_position(rm, entry=0.50, market_id="mkt-a", size=100.0, trader="0xtrader"):
    return await rm.build_position(
        position_id=f"pos-{market_id}-{entry}",
        market_id=market_id,
        token_id=f"tok-{market_id}",
        trader_address=trader,
        entry_price=entry,
        size_shares=size,
    )


class TestPortfolioManager:
    @pytest.mark.asyncio
    async def test_open_and_count(self, portfolio, rm):
        pos = await make_position(rm)
        await portfolio.open_position(pos)
        assert await portfolio.position_count() == 1

    @pytest.mark.asyncio
    async def test_get_position(self, portfolio, rm):
        pos = await make_position(rm)
        await portfolio.open_position(pos)
        fetched = await portfolio.get_position(pos.position_id)
        assert fetched is not None
        assert fetched.entry_price == 0.50
        assert fetched.tp_price == pos.tp_price

    @pytest.mark.asyncio
    async def test_get_position_by_token(self, portfolio, rm):
        pos = await make_position(rm)
        await portfolio.open_position(pos)
        fetched = await portfolio.get_position_by_token(pos.token_id)
        assert fetched is not None
        assert fetched.position_id == pos.position_id

    @pytest.mark.asyncio
    async def test_get_positions_by_token_returns_all(self, portfolio, rm):
        # Two traders copied into separate positions on the SAME token. Both
        # must be returned so per-tick exit evaluation never orphans the second.
        shared = "tok-shared"
        pos_a = await rm.build_position(
            position_id="pos-a", market_id="mkt-a", token_id=shared,
            trader_address="0xA", entry_price=0.50, size_shares=100.0,
        )
        pos_b = await rm.build_position(
            position_id="pos-b", market_id="mkt-a", token_id=shared,
            trader_address="0xB", entry_price=0.50, size_shares=100.0,
        )
        await portfolio.open_position(pos_a)
        await portfolio.open_position(pos_b)

        fetched = await portfolio.get_positions_by_token(shared)
        assert {p.position_id for p in fetched} == {"pos-a", "pos-b"}

    @pytest.mark.asyncio
    async def test_get_positions_by_token_empty_when_none(self, portfolio, rm):
        assert await portfolio.get_positions_by_token("ghost-token") == []

    @pytest.mark.asyncio
    async def test_close_position_profit(self, portfolio, rm):
        pos = await make_position(rm, entry=0.50, size=1000.0)
        await portfolio.open_position(pos)
        pnl = await portfolio.close_position(pos.position_id, 0.60, ExitReason.TAKE_PROFIT)
        assert pnl == pytest.approx(100.0)  # (0.60-0.50)*1000
        assert await portfolio.position_count() == 0

    @pytest.mark.asyncio
    async def test_close_nonexistent(self, portfolio):
        pnl = await portfolio.close_position("ghost", 0.5, ExitReason.STOP_LOSS)
        assert pnl is None

    @pytest.mark.asyncio
    async def test_close_position_break_even(self, portfolio, rm):
        """A genuine break-even close (exit_price == entry_price) returns 0.0, NOT None.
        This distinguishes it from the already-closed sentinel (None) so record_exit
        and metrics are still called for a real (if flat) trade outcome.
        """
        pos = await make_position(rm, entry=0.50, size=100.0)
        await portfolio.open_position(pos)
        pnl = await portfolio.close_position(pos.position_id, 0.50, ExitReason.STOP_LOSS)
        assert pnl == pytest.approx(0.0)   # flat PnL — not None
        assert await portfolio.position_count() == 0
        report = await portfolio.realized_pnl_report()
        assert report["disposals"] == 1    # tax lot must be recorded

    @pytest.mark.asyncio
    async def test_get_open_positions(self, portfolio, rm):
        await portfolio.open_position(await make_position(rm, market_id="a"))
        await portfolio.open_position(await make_position(rm, market_id="b"))
        open_positions = await portfolio.get_open_positions()
        assert len(open_positions) == 2

    @pytest.mark.asyncio
    async def test_update_peak_price(self, portfolio, rm):
        pos = await make_position(rm)
        await portfolio.open_position(pos)
        await portfolio.update_peak_price(pos.position_id, 0.70)
        fetched = await portfolio.get_position(pos.position_id)
        assert fetched.peak_price == 0.70

    @pytest.mark.asyncio
    async def test_trader_pnl_aggregates_closed(self, portfolio, rm):
        pos1 = await make_position(rm, market_id="a", entry=0.50, size=1000.0, trader="0xwhale")
        pos2 = await make_position(rm, market_id="b", entry=0.50, size=1000.0, trader="0xwhale")
        await portfolio.open_position(pos1)
        await portfolio.open_position(pos2)
        await portfolio.close_position(pos1.position_id, 0.60, ExitReason.TAKE_PROFIT)  # +100
        await portfolio.close_position(pos2.position_id, 0.45, ExitReason.STOP_LOSS)    # -50
        total = await portfolio.get_trader_pnl("0xwhale")
        assert total == pytest.approx(50.0)

    @pytest.mark.asyncio
    async def test_trader_win_rate_counts_wins_and_sample(self, portfolio, rm):
        # 2 wins, 1 loss for the trader → win_rate=2/3, sample=3.
        pos1 = await make_position(rm, market_id="a", entry=0.50, size=1000.0, trader="0xw")
        pos2 = await make_position(rm, market_id="b", entry=0.50, size=1000.0, trader="0xw")
        pos3 = await make_position(rm, market_id="c", entry=0.50, size=1000.0, trader="0xw")
        await portfolio.open_position(pos1)
        await portfolio.open_position(pos2)
        await portfolio.open_position(pos3)
        await portfolio.close_position(pos1.position_id, 0.60, ExitReason.TAKE_PROFIT)  # win
        await portfolio.close_position(pos2.position_id, 0.70, ExitReason.TAKE_PROFIT)  # win
        await portfolio.close_position(pos3.position_id, 0.40, ExitReason.STOP_LOSS)    # loss
        win_rate, sample = await portfolio.get_trader_win_rate("0xw")
        assert sample == 3
        assert win_rate == pytest.approx(2 / 3)

    @pytest.mark.asyncio
    async def test_trader_win_rate_empty(self, portfolio):
        win_rate, sample = await portfolio.get_trader_win_rate("0xnobody")
        assert (win_rate, sample) == (0.0, 0)

    @pytest.mark.asyncio
    async def test_trader_win_rate_ignores_open_positions(self, portfolio, rm):
        # Open positions are not yet realized; only closed ones count.
        pos = await make_position(rm, market_id="a", trader="0xw")
        await portfolio.open_position(pos)
        win_rate, sample = await portfolio.get_trader_win_rate("0xw")
        assert (win_rate, sample) == (0.0, 0)

    @pytest.mark.asyncio
    async def test_persistence_across_instances(self, tmp_path, rm):
        db = str(tmp_path / "persist.db")
        pm1 = PortfolioManager(db_path=db)
        await pm1.init()
        pos = await make_position(rm)
        await pm1.open_position(pos)
        await pm1.close()

        pm2 = PortfolioManager(db_path=db)
        await pm2.init()
        assert await pm2.position_count() == 1
        await pm2.close()

    @pytest.mark.asyncio
    async def test_summary(self, portfolio, rm):
        pos = await make_position(rm, size=1000.0)
        await portfolio.open_position(pos)
        await portfolio.close_position(pos.position_id, 0.60, ExitReason.TAKE_PROFIT)
        summary = await portfolio.summary()
        assert "Portfolio Summary" in summary
        assert "Closed trades: 1" in summary


class TestUninitializedGuard:
    """Using a PortfolioManager before `init()` must raise a clear, actionable
    RuntimeError instead of a cryptic AttributeError on a None connection."""

    @pytest.mark.asyncio
    async def test_position_count_before_init_raises(self, rm):
        pm = PortfolioManager(db_path="unused.db")
        with pytest.raises(RuntimeError, match="not initialized"):
            await pm.position_count()

    @pytest.mark.asyncio
    async def test_open_position_before_init_raises(self, rm):
        pm = PortfolioManager(db_path="unused.db")
        with pytest.raises(RuntimeError, match="not initialized"):
            await pm.open_position(await make_position(rm))

    @pytest.mark.asyncio
    async def test_close_before_init_does_not_leak_attribute_error(self, rm):
        pm = PortfolioManager(db_path="unused.db")
        with pytest.raises(RuntimeError, match="not initialized"):
            await pm.get_open_positions()


class TestDoubleExitGuard:
    """C4: close_position must be idempotent — a second call on an already-closed
    position returns None and does NOT insert a second realized-lot row."""

    @pytest.mark.asyncio
    async def test_second_close_returns_none(self, portfolio, rm):
        pos = await make_position(rm, entry=0.50, size=100.0)
        await portfolio.open_position(pos)
        pnl1 = await portfolio.close_position(pos.position_id, 0.60, ExitReason.TAKE_PROFIT)
        pnl2 = await portfolio.close_position(pos.position_id, 0.60, ExitReason.TAKE_PROFIT)
        assert pnl1 == pytest.approx(10.0)
        assert pnl2 is None  # race guard: already closed → None sentinel

    @pytest.mark.asyncio
    async def test_second_close_does_not_add_extra_lot(self, portfolio, rm):
        pos = await make_position(rm, entry=0.50, size=100.0)
        await portfolio.open_position(pos)
        await portfolio.close_position(pos.position_id, 0.60, ExitReason.TAKE_PROFIT)
        await portfolio.close_position(pos.position_id, 0.60, ExitReason.TAKE_PROFIT)
        report = await portfolio.realized_pnl_report()
        # Only one disposal recorded, not two.
        assert report["disposals"] == 1

    @pytest.mark.asyncio
    async def test_concurrent_closes_produce_one_lot(self, portfolio, rm):
        """Simulates a race: two coroutines calling close_position concurrently."""
        import asyncio as _asyncio
        pos = await make_position(rm, entry=0.50, size=100.0)
        await portfolio.open_position(pos)
        pnls = await _asyncio.gather(
            portfolio.close_position(pos.position_id, 0.60, ExitReason.TAKE_PROFIT),
            portfolio.close_position(pos.position_id, 0.60, ExitReason.TAKE_PROFIT),
        )
        # Exactly one winner (10.0 PnL); the loser returns None (race guard).
        non_none = [p for p in pnls if p is not None]
        assert len(non_none) == 1
        assert non_none[0] == pytest.approx(10.0)
        report = await portfolio.realized_pnl_report()
        assert report["disposals"] == 1


class TestRealizedPnlLedger:
    """The tax-lot ledger written on close_position() and its reporting."""

    @pytest.mark.asyncio
    async def test_close_records_realized_lot(self, portfolio, rm):
        pos = await make_position(rm, entry=0.50, size=100.0)  # cost basis $50
        await portfolio.open_position(pos)
        pnl = await portfolio.close_position(pos.position_id, 0.65, ExitReason.TAKE_PROFIT)
        # (0.65 - 0.50) * 100 = $15 realized
        assert pnl == pytest.approx(15.0)

        report = await portfolio.realized_pnl_report()
        assert report["disposals"] == 1
        assert report["proceeds"] == pytest.approx(65.0)       # 0.65 * 100
        assert report["cost_basis"] == pytest.approx(50.0)     # 0.50 * 100
        assert report["net_realized_pnl"] == pytest.approx(15.0)
        # A just-opened position is short-term.
        assert report["short_term_pnl"] == pytest.approx(15.0)
        assert report["long_term_pnl"] == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_report_aggregates_multiple_disposals(self, portfolio, rm):
        win = await make_position(rm, entry=0.40, market_id="mkt-w", size=100.0)
        loss = await make_position(rm, entry=0.60, market_id="mkt-l", size=100.0)
        await portfolio.open_position(win)
        await portfolio.open_position(loss)
        await portfolio.close_position(win.position_id, 0.55, ExitReason.TAKE_PROFIT)   # +15
        await portfolio.close_position(loss.position_id, 0.50, ExitReason.STOP_LOSS)    # -10

        report = await portfolio.realized_pnl_report()
        assert report["disposals"] == 2
        assert report["net_realized_pnl"] == pytest.approx(5.0)

    @pytest.mark.asyncio
    async def test_long_term_lot_classified(self, portfolio, rm):
        pos = await make_position(rm, entry=0.50, size=100.0)
        pos.entry_time = pos.entry_time - 400 * 86_400  # held > 1 year
        await portfolio.open_position(pos)
        await portfolio.close_position(pos.position_id, 0.60, ExitReason.TAKE_PROFIT)

        report = await portfolio.realized_pnl_report()
        assert report["long_term_pnl"] == pytest.approx(10.0)
        assert report["short_term_pnl"] == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_report_filters_by_year(self, portfolio, rm):
        from datetime import datetime, timezone

        pos = await make_position(rm, entry=0.50, size=100.0)
        await portfolio.open_position(pos)
        await portfolio.close_position(pos.position_id, 0.60, ExitReason.TAKE_PROFIT)

        this_year = datetime.now(timezone.utc).year
        assert (await portfolio.realized_pnl_report(year=this_year))["disposals"] == 1
        # A year with no disposals is empty, not an error.
        assert (await portfolio.realized_pnl_report(year=this_year - 5))["disposals"] == 0

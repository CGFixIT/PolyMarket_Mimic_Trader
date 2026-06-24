"""Portfolio state tracking with SQLite persistence (aiosqlite, WAL mode)."""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import List, Optional

import aiosqlite

from polymarket_copier.core.risk_manager import ExitReason, Position, Side

logger = logging.getLogger("polymarket_copier")

# US long-term capital-gains threshold: assets held > 1 year. Prediction-market
# positions are almost always short-term, but tracking the split is the whole
# point of a tax-lot ledger, so the boundary is recorded per disposal.
_LONG_TERM_HOLDING_SECONDS = 365 * 86_400

_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    position_id     TEXT PRIMARY KEY,
    market_id       TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    trader_address  TEXT NOT NULL,
    entry_price     REAL NOT NULL,
    tp_price        REAL NOT NULL,
    sl_price        REAL NOT NULL,
    peak_price      REAL NOT NULL,
    size_shares     REAL NOT NULL,
    entry_time      REAL NOT NULL,
    resolve_time    REAL,
    status          TEXT NOT NULL DEFAULT 'open',
    exit_price      REAL,
    exit_reason     TEXT,
    realized_pnl    REAL,
    closed_at       REAL
);

-- Realized-PnL tax-lot ledger. One immutable row per disposal (position close),
-- capturing cost basis, proceeds, holding period and short/long-term character.
-- This is the foundation for year-end realized-gain reporting; the positions
-- table alone only keeps the latest realized_pnl and can't be aggregated by
-- tax year or holding term.
CREATE TABLE IF NOT EXISTS realized_lots (
    lot_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id     TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    trader_address  TEXT NOT NULL,
    shares          REAL NOT NULL,
    cost_basis      REAL NOT NULL,   -- USDC paid to acquire (entry_price * shares)
    proceeds        REAL NOT NULL,   -- USDC received on disposal (exit_price * shares)
    realized_pnl    REAL NOT NULL,   -- proceeds - cost_basis
    acquired_at     REAL NOT NULL,   -- position entry_time (Unix)
    disposed_at     REAL NOT NULL,   -- close time (Unix)
    holding_seconds REAL NOT NULL,
    term            TEXT NOT NULL     -- 'short' or 'long'
);
CREATE INDEX IF NOT EXISTS idx_realized_lots_disposed ON realized_lots (disposed_at);
"""


class PortfolioManager:
    """SQLite-backed store of open and closed copy-trade positions."""

    def __init__(self, db_path: str = "data/positions.db"):
        self._db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def init(self) -> None:
        """Open the SQLite connection, enable WAL mode, and create the schema if absent."""
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute("PRAGMA journal_mode=WAL;")
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        logger.info("Portfolio DB initialized: %s", self._db_path)

    async def close(self) -> None:
        """Close the underlying SQLite connection if it is open."""
        if self._db:
            await self._db.close()

    def _require_db(self) -> aiosqlite.Connection:
        """Return the live connection or fail loudly if init() was never awaited.

        Without this guard, every query below would raise a cryptic
        ``AttributeError: 'NoneType' object has no attribute 'execute'`` that
        gives no hint about the real cause (a missing ``await portfolio.init()``).
        """
        if self._db is None:
            raise RuntimeError("PortfolioManager is not initialized. Call `await portfolio.init()` before using it.")
        return self._db

    async def open_position(self, pos: Position) -> None:
        """Insert a new open position row into the positions table and commit."""
        db = self._require_db()
        await db.execute(
            """INSERT INTO positions
               (position_id, market_id, token_id, trader_address, entry_price,
                tp_price, sl_price, peak_price, size_shares, entry_time, resolve_time, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
            (
                pos.position_id,
                pos.market_id,
                pos.token_id,
                pos.trader_address,
                pos.entry_price,
                pos.tp_price,
                pos.sl_price,
                pos.peak_price,
                pos.size_shares,
                pos.entry_time,
                pos.resolve_time,
            ),
        )
        await db.commit()
        logger.info("Position opened in DB: %s", pos.position_id)

    async def close_position(
        self,
        position_id: str,
        exit_price: float,
        reason: ExitReason,
    ) -> Optional[float]:
        """Mark a position closed (guarded against double-close), record a tax lot, and return realized PnL.

        Returns the realized PnL (float, possibly 0.0 on a genuine break-even) on success,
        or None if the position was not found or was already closed by a concurrent exit.
        Callers must treat None as "already handled" and skip record_exit / metrics.
        """
        db = self._require_db()
        pos = await self.get_position(position_id)
        if pos is None:
            logger.warning("Position %s not found for closing", position_id)
            return None
        pnl = pos.pnl_at(exit_price)
        closed_at = time.time()
        # C4 guard: `AND status='open'` makes this UPDATE a no-op if another
        # coroutine already closed the position (WS tick race, poll sweep, or
        # SOURCE_EXIT arriving simultaneously). The rowcount check prevents
        # double-recording the tax lot and double-calling record_exit.
        cur = await db.execute(
            """UPDATE positions SET status='closed', exit_price=?, exit_reason=?,
               realized_pnl=?, closed_at=? WHERE position_id=? AND status='open'""",
            (exit_price, reason.name, pnl, closed_at, position_id),
        )
        if cur.rowcount != 1:
            logger.warning("Position %s already closed — double-exit race prevented", position_id)
            return None
        # Record an immutable tax lot in the SAME transaction as the close, so the
        # ledger can never drift from the positions table.
        cost_basis = pos.entry_price * pos.size_shares
        proceeds = exit_price * pos.size_shares
        holding_seconds = max(0.0, closed_at - pos.entry_time)
        term = "long" if holding_seconds > _LONG_TERM_HOLDING_SECONDS else "short"
        await db.execute(
            """INSERT INTO realized_lots
               (position_id, token_id, trader_address, shares, cost_basis,
                proceeds, realized_pnl, acquired_at, disposed_at,
                holding_seconds, term)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                position_id,
                pos.token_id,
                pos.trader_address,
                pos.size_shares,
                cost_basis,
                proceeds,
                pnl,
                pos.entry_time,
                closed_at,
                holding_seconds,
                term,
            ),
        )
        await db.commit()
        logger.info("Position closed: %s reason=%s pnl=%.4f", position_id, reason.name, pnl)
        return pnl

    async def realized_pnl_report(self, year: Optional[int] = None) -> dict:
        """Aggregate the realized-lot ledger into a tax-style summary.

        Pass ``year`` (UTC) to scope to a single tax year by disposal date;
        omit it for an all-time report. Returns total proceeds, cost basis,
        net realized PnL, the short/long-term split, and disposal count.
        """
        db = self._require_db()
        where = ""
        params: tuple = ()
        if year is not None:
            start = datetime(year, 1, 1, tzinfo=timezone.utc).timestamp()
            end = datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp()
            where = "WHERE disposed_at >= ? AND disposed_at < ?"
            params = (start, end)
        cursor = await db.execute(
            f"""SELECT
                   COUNT(*),
                   COALESCE(SUM(proceeds), 0),
                   COALESCE(SUM(cost_basis), 0),
                   COALESCE(SUM(realized_pnl), 0),
                   COALESCE(SUM(CASE WHEN term='short' THEN realized_pnl ELSE 0 END), 0),
                   COALESCE(SUM(CASE WHEN term='long'  THEN realized_pnl ELSE 0 END), 0)
               FROM realized_lots {where}""",
            params,
        )
        row = await cursor.fetchone()
        return {
            "year": year,
            "disposals": int(row[0]) if row else 0,
            "proceeds": float(row[1]) if row else 0.0,
            "cost_basis": float(row[2]) if row else 0.0,
            "net_realized_pnl": float(row[3]) if row else 0.0,
            "short_term_pnl": float(row[4]) if row else 0.0,
            "long_term_pnl": float(row[5]) if row else 0.0,
        }

    async def get_open_positions(self) -> List[Position]:
        """Return all positions whose status is 'open'."""
        db = self._require_db()
        cursor = await db.execute("SELECT * FROM positions WHERE status='open'")
        rows = await cursor.fetchall()
        return [_row_to_position(row) for row in rows]

    async def get_position(self, position_id: str) -> Optional[Position]:
        """Return the position with the given id, or None if not found."""
        db = self._require_db()
        cursor = await db.execute("SELECT * FROM positions WHERE position_id=?", (position_id,))
        row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_position(row)

    async def get_position_by_token(self, token_id: str) -> Optional[Position]:
        """Return a single open position for the given token id, or None if none exists."""
        db = self._require_db()
        cursor = await db.execute("SELECT * FROM positions WHERE token_id=? AND status='open'", (token_id,))
        row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_position(row)

    async def get_positions_by_token(self, token_id: str) -> List[Position]:
        """Return ALL open positions on a token.

        Two different tracked traders can each be copied into a SEPARATE
        position (unique position_id) on the same token. The singular
        get_position_by_token() returns only one of them, so per-tick exit
        evaluation and source-exit mirroring must use this plural query to
        avoid orphaning the second (and beyond) position on a shared token.
        """
        db = self._require_db()
        cursor = await db.execute("SELECT * FROM positions WHERE token_id=? AND status='open'", (token_id,))
        rows = await cursor.fetchall()
        return [_row_to_position(row) for row in rows]

    async def position_count(self) -> int:
        """Return the number of currently open positions."""
        db = self._require_db()
        cursor = await db.execute("SELECT COUNT(*) FROM positions WHERE status='open'")
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def update_peak_price(self, position_id: str, peak_price: float) -> None:
        """Update the stored peak_price for a position (used for trailing-stop tracking)."""
        db = self._require_db()
        await db.execute(
            "UPDATE positions SET peak_price=? WHERE position_id=?",
            (peak_price, position_id),
        )
        await db.commit()

    async def get_open_unrealized_pnl_conservative(self) -> float:
        """Return sum of (sl_price - entry_price)*size_shares for all open positions.
        This is always <= 0 and represents the maximum realizable loss if all stops
        are hit — a conservative mark-to-market for the daily-loss breaker."""
        positions = await self.get_open_positions()
        return sum(pos.pnl_at(pos.sl_price) for pos in positions)

    async def get_trader_pnl(self, trader_address: str) -> float:
        """Return the summed realized PnL of all closed positions copied from a trader."""
        db = self._require_db()
        cursor = await db.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) FROM positions WHERE trader_address=? AND status='closed'",
            (trader_address,),
        )
        row = await cursor.fetchone()
        return float(row[0]) if row else 0.0

    async def get_trader_win_rate(self, trader_address: str) -> tuple[float, int]:
        """Return (win_rate, sample_size) over CLOSED positions for a trader.

        A closed position with ``realized_pnl > 0`` counts as a win. The win
        rate is wins / sample_size in [0, 1]. With no closed positions for the
        trader, returns ``(0.0, 0)`` so callers can gate on sample size.
        """
        db = self._require_db()
        cursor = await db.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) "
            "FROM positions WHERE trader_address=? AND status='closed'",
            (trader_address,),
        )
        row = await cursor.fetchone()
        total = int(row[0]) if row and row[0] is not None else 0
        wins = int(row[1]) if row and row[1] is not None else 0
        if total == 0:
            return 0.0, 0
        return wins / total, total

    async def summary(self) -> str:
        """Return a formatted text summary of open count, closed trades, win rate, and realized PnL."""
        db = self._require_db()
        open_count = await self.position_count()
        cursor = await db.execute(
            "SELECT COUNT(*), COALESCE(SUM(realized_pnl), 0), "
            "SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) "
            "FROM positions WHERE status='closed'"
        )
        row = await cursor.fetchone()
        total = row[0] if row else 0
        realized = row[1] if row else 0
        wins = row[2] if row and row[2] is not None else 0
        wr = (wins / total * 100) if total > 0 else 0
        return (
            "=== Portfolio Summary ===\n"
            f"Open positions: {open_count}\n"
            f"Closed trades: {total}\n"
            f"Win rate: {wr:.1f}%\n"
            f"Realized P&L: ${realized:.2f}"
        )


def _row_to_position(row) -> Position:
    return Position(
        position_id=row[0],
        market_id=row[1],
        token_id=row[2],
        trader_address=row[3],
        entry_price=row[4],
        tp_price=row[5],
        sl_price=row[6],
        peak_price=row[7],
        size_shares=row[8],
        entry_time=row[9],
        resolve_time=row[10],
        side=Side.BUY,
    )

"""Portfolio state tracking with SQLite persistence (aiosqlite, WAL mode)."""

from __future__ import annotations

import logging
import os
import time
from typing import List, Optional

import aiosqlite

from polymarket_copier.core.risk_manager import ExitReason, Position, Side

logger = logging.getLogger("polymarket_copier")

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
"""


class PortfolioManager:
    """SQLite-backed store of open and closed copy-trade positions."""

    def __init__(self, db_path: str = "data/positions.db"):
        self._db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def init(self) -> None:
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute("PRAGMA journal_mode=WAL;")
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        logger.info("Portfolio DB initialized: %s", self._db_path)

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    def _require_db(self) -> aiosqlite.Connection:
        """Return the live connection or fail loudly if init() was never awaited.

        Without this guard, every query below would raise a cryptic
        ``AttributeError: 'NoneType' object has no attribute 'execute'`` that
        gives no hint about the real cause (a missing ``await portfolio.init()``).
        """
        if self._db is None:
            raise RuntimeError(
                "PortfolioManager is not initialized. "
                "Call `await portfolio.init()` before using it."
            )
        return self._db

    async def open_position(self, pos: Position) -> None:
        db = self._require_db()
        await db.execute(
            """INSERT INTO positions
               (position_id, market_id, token_id, trader_address, entry_price,
                tp_price, sl_price, peak_price, size_shares, entry_time, resolve_time, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
            (pos.position_id, pos.market_id, pos.token_id, pos.trader_address,
             pos.entry_price, pos.tp_price, pos.sl_price, pos.peak_price,
             pos.size_shares, pos.entry_time, pos.resolve_time),
        )
        await db.commit()
        logger.info("Position opened in DB: %s", pos.position_id)

    async def close_position(
        self, position_id: str, exit_price: float, reason: ExitReason,
    ) -> float:
        db = self._require_db()
        pos = await self.get_position(position_id)
        if pos is None:
            logger.warning("Position %s not found for closing", position_id)
            return 0.0
        pnl = pos.pnl_at(exit_price)
        await db.execute(
            """UPDATE positions SET status='closed', exit_price=?, exit_reason=?,
               realized_pnl=?, closed_at=? WHERE position_id=?""",
            (exit_price, reason.name, pnl, time.time(), position_id),
        )
        await db.commit()
        logger.info("Position closed: %s reason=%s pnl=%.4f", position_id, reason.name, pnl)
        return pnl

    async def get_open_positions(self) -> List[Position]:
        db = self._require_db()
        cursor = await db.execute("SELECT * FROM positions WHERE status='open'")
        rows = await cursor.fetchall()
        return [_row_to_position(row) for row in rows]

    async def get_position(self, position_id: str) -> Optional[Position]:
        db = self._require_db()
        cursor = await db.execute(
            "SELECT * FROM positions WHERE position_id=?", (position_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_position(row)

    async def get_position_by_token(self, token_id: str) -> Optional[Position]:
        db = self._require_db()
        cursor = await db.execute(
            "SELECT * FROM positions WHERE token_id=? AND status='open'", (token_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_position(row)

    async def position_count(self) -> int:
        db = self._require_db()
        cursor = await db.execute(
            "SELECT COUNT(*) FROM positions WHERE status='open'"
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def update_peak_price(self, position_id: str, peak_price: float) -> None:
        db = self._require_db()
        await db.execute(
            "UPDATE positions SET peak_price=? WHERE position_id=?",
            (peak_price, position_id),
        )
        await db.commit()

    async def get_trader_pnl(self, trader_address: str) -> float:
        db = self._require_db()
        cursor = await db.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) FROM positions "
            "WHERE trader_address=? AND status='closed'",
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
        db = self._require_db()
        open_count = await self.position_count()
        cursor = await db.execute(
            "SELECT COUNT(*), COALESCE(SUM(realized_pnl), 0), "
            "SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) "
            "FROM positions WHERE status='closed'"
        )
        row = await cursor.fetchone()
        total    = row[0] if row else 0
        realized = row[1] if row else 0
        wins     = row[2] if row and row[2] is not None else 0
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

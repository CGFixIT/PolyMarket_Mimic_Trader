"""Portfolio state tracking and persistence."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from polymarket_copier.models.types import ExitReason, Position, TradeSide

logger = logging.getLogger("polymarket_copier")

DEFAULT_STATE_FILE = "portfolio_state.json"


class Portfolio:
    """Tracks all open positions and provides P&L reporting."""

    def __init__(self, state_file: str = DEFAULT_STATE_FILE):
        self.positions: dict[str, Position] = {}
        self.closed_pnl: float = 0.0
        self.total_trades: int = 0
        self.winning_trades: int = 0
        self.state_file = state_file

    def add_position(self, position: Position) -> None:
        """Add a new open position."""
        self.positions[position.id] = position
        self.total_trades += 1
        logger.info(
            "Position opened: %s %s %.4f @ %.4f on %s (from %s)",
            position.side.value,
            position.outcome or position.asset_id[:10],
            position.size,
            position.entry_price,
            position.market_slug or position.market_id[:10],
            position.source_trader[:10],
        )
        self.save()

    def close_position(self, position_id: str, exit_price: float, reason: ExitReason) -> Optional[float]:
        """Close a position and return the realized P&L."""
        position = self.positions.pop(position_id, None)
        if position is None:
            logger.warning("Position %s not found for closing", position_id)
            return None

        position.current_price = exit_price
        pnl = position.unrealized_pnl
        self.closed_pnl += pnl

        if pnl > 0:
            self.winning_trades += 1

        logger.info(
            "Position closed [%s]: %s %.4f @ %.4f -> %.4f | PnL=$%.2f (%.1f%%)",
            reason.value,
            position.side.value,
            position.size,
            position.entry_price,
            exit_price,
            pnl,
            position.unrealized_pnl_pct * 100,
        )
        self.save()
        return pnl

    def update_prices(self, prices: dict[str, float]) -> None:
        """Update current prices for all open positions."""
        for pos in self.positions.values():
            price = prices.get(pos.asset_id) or prices.get(pos.market_id)
            if price is not None:
                pos.update_price(price)

    @property
    def open_positions(self) -> list[Position]:
        return list(self.positions.values())

    @property
    def unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self.positions.values())

    @property
    def total_pnl(self) -> float:
        return self.closed_pnl + self.unrealized_pnl

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.winning_trades / self.total_trades

    def get_trader_positions(self, trader_address: str) -> list[Position]:
        """Get all open positions for a specific followed trader."""
        return [p for p in self.positions.values() if p.source_trader == trader_address]

    def save(self) -> None:
        """Persist portfolio state to JSON file."""
        state = {
            "positions": {pid: p.model_dump() for pid, p in self.positions.items()},
            "closed_pnl": self.closed_pnl,
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
        }
        try:
            Path(self.state_file).write_text(json.dumps(state, indent=2, default=str))
        except Exception as e:
            logger.error("Failed to save portfolio state: %s", e)

    def load(self) -> None:
        """Load portfolio state from JSON file."""
        path = Path(self.state_file)
        if not path.exists():
            return
        try:
            state = json.loads(path.read_text())
            self.closed_pnl = state.get("closed_pnl", 0.0)
            self.total_trades = state.get("total_trades", 0)
            self.winning_trades = state.get("winning_trades", 0)
            for pid, pdata in state.get("positions", {}).items():
                self.positions[pid] = Position(**pdata)
            logger.info("Loaded %d positions from state file", len(self.positions))
        except Exception as e:
            logger.error("Failed to load portfolio state: %s", e)

    def summary(self) -> str:
        """Return a human-readable portfolio summary."""
        lines = [
            "=== Portfolio Summary ===",
            f"Open positions: {len(self.positions)}",
            f"Total trades: {self.total_trades}",
            f"Win rate: {self.win_rate:.1%}",
            f"Realized P&L: ${self.closed_pnl:.2f}",
            f"Unrealized P&L: ${self.unrealized_pnl:.2f}",
            f"Total P&L: ${self.total_pnl:.2f}",
        ]
        for p in self.positions.values():
            lines.append(
                f"  {p.side.value} {p.outcome or p.asset_id[:10]} | "
                f"Size={p.size:.4f} | Entry={p.entry_price:.4f} | "
                f"Current={p.current_price:.4f} | PnL={p.unrealized_pnl_pct:.1%}"
            )
        return "\n".join(lines)

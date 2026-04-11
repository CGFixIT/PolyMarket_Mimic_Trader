"""Top trader discovery and scoring."""

from __future__ import annotations

import logging
from typing import Any

from polymarket_copier.api.data_client import DataClient
from polymarket_copier.config import TraderSelectionConfig
from polymarket_copier.models.types import Trader

logger = logging.getLogger("polymarket_copier")


class TraderTracker:
    """Discovers and ranks top traders from the Polymarket leaderboard."""

    def __init__(self, data_client: DataClient, config: TraderSelectionConfig, max_traders: int = 5):
        self.data_client = data_client
        self.config = config
        self.max_traders = max_traders
        self.tracked_traders: list[Trader] = []

    def parse_trader(self, raw: dict[str, Any]) -> Trader:
        """Parse a raw leaderboard entry into a Trader model."""
        address = raw.get("address") or raw.get("userAddress") or raw.get("wallet", "")
        pnl = float(raw.get("pnl") or raw.get("profit") or raw.get("totalPnl") or 0)
        volume = float(raw.get("volume") or raw.get("totalVolume") or 0)

        total_trades = int(raw.get("numTrades") or raw.get("totalTrades") or raw.get("trades") or 0)
        wins = int(raw.get("wins") or raw.get("numWins") or 0)
        losses = int(raw.get("losses") or raw.get("numLosses") or 0)

        if total_trades > 0 and wins == 0 and losses == 0:
            win_rate = float(raw.get("winRate") or raw.get("win_rate") or 0)
        elif (wins + losses) > 0:
            win_rate = wins / (wins + losses)
        else:
            win_rate = 0.0

        return Trader(
            address=address,
            pnl=pnl,
            win_rate=win_rate,
            total_trades=total_trades,
            volume=volume,
        )

    def filter_traders(self, traders: list[Trader]) -> list[Trader]:
        """Filter traders by minimum thresholds."""
        return [
            t for t in traders
            if t.pnl >= self.config.min_pnl
            and t.win_rate >= self.config.min_win_rate
            and t.total_trades >= self.config.min_trades
            and t.address
        ]

    def rank_traders(self, traders: list[Trader]) -> list[Trader]:
        """Score and rank traders, returning top N."""
        for t in traders:
            t.compute_score()
        traders.sort(key=lambda t: t.score, reverse=True)
        return traders[: self.max_traders]

    async def discover(self) -> list[Trader]:
        """Fetch leaderboard, filter, rank, and return top traders."""
        raw_data = await self.data_client.get_leaderboard(
            period="all",
            order_by="pnl",
            limit=100,
        )

        traders = [self.parse_trader(entry) for entry in raw_data]
        filtered = self.filter_traders(traders)

        if not filtered:
            logger.warning("No traders meet minimum thresholds, relaxing filters")
            filtered = [t for t in traders if t.address and t.pnl > 0]

        ranked = self.rank_traders(filtered)
        self.tracked_traders = ranked

        logger.info(
            "Discovered %d traders (from %d candidates, %d filtered)",
            len(ranked),
            len(traders),
            len(filtered),
        )
        for t in ranked:
            logger.info(
                "  Tracker: %s | PnL=$%.2f | WR=%.1f%% | Trades=%d | Score=%.3f",
                t.address[:10] + "...",
                t.pnl,
                t.win_rate * 100,
                t.total_trades,
                t.score,
            )

        return ranked

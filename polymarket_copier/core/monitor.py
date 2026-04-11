"""Trade monitor — polls tracked wallets for new trades."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Coroutine, Optional

from polymarket_copier.api.data_client import DataClient
from polymarket_copier.models.types import Trade, TradeSide

logger = logging.getLogger("polymarket_copier")


class TradeMonitor:
    """Polls the Data API for new trades from tracked wallets."""

    def __init__(
        self,
        data_client: DataClient,
        trader_addresses: list[str],
        poll_interval: int = 20,
        on_trade: Optional[Callable[[Trade], Coroutine[Any, Any, None]]] = None,
    ):
        self.data_client = data_client
        self.trader_addresses = trader_addresses
        self.poll_interval = poll_interval
        self.on_trade = on_trade
        self._seen_trade_ids: set[str] = set()
        self._running = False
        self._last_poll: dict[str, float] = {}

    def parse_trade(self, raw: dict[str, Any], trader_address: str) -> Optional[Trade]:
        """Parse a raw trade dict into a Trade model."""
        trade_id = str(raw.get("id") or raw.get("tradeId") or raw.get("transactionHash") or "")
        if not trade_id:
            return None

        side_raw = str(raw.get("side") or raw.get("type") or "").upper()
        side = TradeSide.BUY if side_raw in ("BUY", "LONG", "YES") else TradeSide.SELL

        return Trade(
            id=trade_id,
            trader_address=trader_address,
            market_id=str(raw.get("market") or raw.get("marketId") or raw.get("conditionId") or ""),
            asset_id=str(raw.get("asset_id") or raw.get("assetId") or raw.get("tokenId") or ""),
            side=side,
            size=float(raw.get("size") or raw.get("amount") or 0),
            price=float(raw.get("price") or raw.get("avgPrice") or 0),
            timestamp=float(raw.get("timestamp") or raw.get("createdAt") or time.time()),
            market_slug=str(raw.get("slug") or raw.get("marketSlug") or ""),
            outcome=str(raw.get("outcome") or raw.get("outcomeName") or ""),
        )

    async def poll_trader(self, address: str) -> list[Trade]:
        """Poll for new trades from a single trader. Returns newly detected trades."""
        new_trades: list[Trade] = []
        try:
            raw_trades = await self.data_client.get_trades(trader=address, limit=20)
        except Exception as e:
            logger.error("Failed to poll trades for %s: %s", address[:10], e)
            return new_trades

        for raw in raw_trades:
            trade = self.parse_trade(raw, address)
            if trade is None:
                continue
            if trade.id in self._seen_trade_ids:
                continue

            self._seen_trade_ids.add(trade.id)
            new_trades.append(trade)

        if new_trades:
            logger.info("Detected %d new trade(s) from %s", len(new_trades), address[:10])

        self._last_poll[address] = time.time()
        return new_trades

    async def poll_all(self) -> list[Trade]:
        """Poll all tracked traders concurrently."""
        tasks = [self.poll_trader(addr) for addr in self.trader_addresses]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_new: list[Trade] = []
        for result in results:
            if isinstance(result, Exception):
                logger.error("Poll error: %s", result)
                continue
            all_new.extend(result)

        return all_new

    async def run(self) -> None:
        """Run the monitor loop, polling at the configured interval."""
        self._running = True
        logger.info("Trade monitor started — tracking %d wallets", len(self.trader_addresses))

        # Initial poll to seed seen trade IDs (don't trigger copies for old trades)
        logger.info("Seeding initial trade history...")
        await self.poll_all()
        logger.info("Seeded %d historical trade IDs", len(self._seen_trade_ids))

        while self._running:
            new_trades = await self.poll_all()
            for trade in new_trades:
                if self.on_trade:
                    await self.on_trade(trade)
            await asyncio.sleep(self.poll_interval)

    def stop(self) -> None:
        """Stop the monitor loop."""
        self._running = False
        logger.info("Trade monitor stopped")

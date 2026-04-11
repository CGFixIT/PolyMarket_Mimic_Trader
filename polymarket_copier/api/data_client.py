"""Client for the Polymarket Data API (no authentication required)."""

from __future__ import annotations

import logging
from typing import Any, Optional

import aiohttp

logger = logging.getLogger("polymarket_copier")

DATA_API_BASE = "https://data-api.polymarket.com"


class DataClient:
    """Wraps the Polymarket Data API for leaderboard, trades, and activity data."""

    def __init__(self, base_url: str = DATA_API_BASE, session: Optional[aiohttp.ClientSession] = None):
        self.base_url = base_url.rstrip("/")
        self._external_session = session is not None
        self._session = session

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._external_session:
            await self._session.close()

    async def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        session = await self._get_session()
        url = f"{self.base_url}{path}"
        logger.debug("GET %s params=%s", url, params)
        async with session.get(url, params=params) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_leaderboard(
        self,
        period: str = "all",
        order_by: str = "pnl",
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Fetch top traders from the leaderboard.

        Args:
            period: Time period - "all", "1d", "7d", "30d"
            order_by: Sort field - "pnl", "volume"
            limit: Number of results
            offset: Pagination offset
        """
        params = {
            "period": period,
            "sortBy": order_by,
            "limit": limit,
            "offset": offset,
        }
        data = await self._get("/leaderboard", params=params)
        if isinstance(data, list):
            return data
        return data.get("leaderboard", data.get("data", []))

    async def get_trades(
        self,
        trader: Optional[str] = None,
        market: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Fetch recent trades, optionally filtered by trader address or market."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if trader:
            params["maker"] = trader
        if market:
            params["market"] = market
        data = await self._get("/trades", params=params)
        if isinstance(data, list):
            return data
        return data.get("trades", data.get("data", []))

    async def get_activity(self, address: str, limit: int = 50) -> list[dict[str, Any]]:
        """Fetch on-chain activity for a specific address."""
        params: dict[str, Any] = {"address": address, "limit": limit}
        data = await self._get("/activity", params=params)
        if isinstance(data, list):
            return data
        return data.get("activity", data.get("data", []))

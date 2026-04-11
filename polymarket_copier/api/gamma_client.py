"""Client for the Polymarket Gamma API (market discovery, no auth required)."""

from __future__ import annotations

import logging
from typing import Any, Optional

import aiohttp

logger = logging.getLogger("polymarket_copier")

GAMMA_API_BASE = "https://gamma-api.polymarket.com"


class GammaClient:
    """Wraps the Polymarket Gamma API for market and event discovery."""

    def __init__(self, base_url: str = GAMMA_API_BASE, session: Optional[aiohttp.ClientSession] = None):
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

    async def get_markets(self, limit: int = 50, active: bool = True) -> list[dict[str, Any]]:
        """Fetch available markets."""
        params: dict[str, Any] = {"limit": limit}
        if active:
            params["active"] = "true"
        data = await self._get("/markets", params=params)
        if isinstance(data, list):
            return data
        return data.get("markets", data.get("data", []))

    async def get_market(self, market_id: str) -> dict[str, Any]:
        """Fetch a single market by ID or slug."""
        data = await self._get(f"/markets/{market_id}")
        if isinstance(data, dict):
            return data
        return {}

    async def get_events(self, limit: int = 50) -> list[dict[str, Any]]:
        """Fetch events (groups of related markets)."""
        data = await self._get("/events", params={"limit": limit})
        if isinstance(data, list):
            return data
        return data.get("events", data.get("data", []))

    async def get_market_price(self, token_id: str) -> Optional[float]:
        """Get the current mid price for a token."""
        try:
            data = await self._get(f"/markets/{token_id}")
            if isinstance(data, dict):
                price = data.get("midpoint") or data.get("lastTradePrice") or data.get("price")
                if price is not None:
                    return float(price)
        except Exception:
            logger.warning("Failed to get price for token %s", token_id)
        return None

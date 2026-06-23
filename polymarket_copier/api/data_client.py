"""Client for the Polymarket Data API (no authentication required)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import aiohttp
from aiolimiter import AsyncLimiter

logger = logging.getLogger("polymarket_copier")

DATA_API_BASE = "https://data-api.polymarket.com"

# Reuse keep-alive connections across the steady stream of leaderboard/activity
# polls instead of re-handshaking TLS on every request.
_CONN_LIMIT = 20
_KEEPALIVE_TIMEOUT = 30


class DataClient:
    """Wraps the Polymarket Data API for leaderboard, trades, and activity data."""

    def __init__(self, base_url: str = DATA_API_BASE, session: Optional[aiohttp.ClientSession] = None):
        self.base_url = base_url.rstrip("/")
        self._external_session = session is not None
        self._session = session
        self._limiter = AsyncLimiter(30, 60)  # 30 requests per 60 seconds
        # Guards lazy session creation against concurrent first-callers (see
        # GammaClient for the orphaned-session race this prevents).
        self._session_lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        # Fast path: an open session already exists, no lock needed.
        if self._session is not None and not self._session.closed:
            return self._session
        async with self._session_lock:
            # Re-check inside the lock — a racing caller may have just built it.
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=10),
                    connector=aiohttp.TCPConnector(
                        limit=_CONN_LIMIT, keepalive_timeout=_KEEPALIVE_TIMEOUT
                    ),
                )
        return self._session

    async def close(self) -> None:
        if self._session and not self._external_session:
            await self._session.close()

    async def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        async with self._limiter:
            session = await self._get_session()
            url = f"{self.base_url}{path}"
            logger.debug("GET %s params=%s", url, params)
            async with session.get(url, params=params) as resp:
                resp.raise_for_status()
                return await resp.json()

    async def get_leaderboard(self, limit: int = 50) -> list[dict[str, Any]]:
        """Fetch top traders from the leaderboard, ranked by all-time PnL."""
        params = {"window": "all", "limit": limit}
        data = await self._get("/leaderboard", params=params)
        if isinstance(data, list):
            return data
        return data.get("leaderboard", data.get("data", []))

    async def get_wallet_activity(self, address: str, limit: int = 100) -> list[dict[str, Any]]:
        """Fetch on-chain trade activity for a specific wallet address."""
        params: dict[str, Any] = {"user": address, "limit": limit}
        data = await self._get("/activity", params=params)
        if isinstance(data, list):
            return data
        return data.get("activity", data.get("data", []))

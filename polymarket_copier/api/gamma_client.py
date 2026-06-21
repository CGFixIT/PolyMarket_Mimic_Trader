"""Client for the Polymarket Gamma API (market discovery, no auth required)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp

from polymarket_copier.models.types import Market

logger = logging.getLogger("polymarket_copier")

GAMMA_API_BASE = "https://gamma-api.polymarket.com"
# Price-by-token-id is a CLOB concept, not a Gamma one — see get_market_price.
CLOB_API_BASE = "https://clob.polymarket.com"


class GammaClient:
    """Wraps the Polymarket Gamma API for market and event discovery."""

    def __init__(self, base_url: str = GAMMA_API_BASE, session: Optional[aiohttp.ClientSession] = None):
        self.base_url = base_url.rstrip("/")
        self._external_session = session is not None
        self._session = session

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
            )
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

    async def get_active_markets(self, limit: int = 100) -> list[Market]:
        """Fetch active markets as typed Market objects (with resolve_time)."""
        params: dict[str, Any] = {"limit": limit, "active": "true"}
        data = await self._get("/markets", params=params)
        raw_list = data if isinstance(data, list) else data.get("markets", data.get("data", []))
        return [_parse_market(raw) for raw in raw_list]

    async def get_market(self, condition_id: str) -> Optional[Market]:
        """Fetch a single market by condition ID or slug."""
        try:
            data = await self._get(f"/markets/{condition_id}")
            if isinstance(data, dict):
                return _parse_market(data)
        except Exception:
            logger.warning("Failed to fetch market %s", condition_id)
        return None

    async def get_market_price(self, token_id: str) -> Optional[float]:
        """Get the current mid price for an outcome token.

        The Gamma /markets/{id} endpoint keys on condition/market id, so querying
        it with an outcome *token* id always misses and returns None. The CLOB
        midpoint endpoint (GET /midpoint?token_id=...) is the correct, no-auth
        source for a token's current price.
        """
        session = await self._get_session()
        url = f"{CLOB_API_BASE}/midpoint"
        try:
            async with session.get(url, params={"token_id": token_id}) as resp:
                resp.raise_for_status()
                data = await resp.json()
            if isinstance(data, dict):
                price = data.get("mid") or data.get("midpoint") or data.get("price")
                if price is not None:
                    return float(price)
        except Exception:
            logger.warning("Failed to get price for token %s", token_id)
        return None


def _parse_resolve_time(raw: dict) -> Optional[datetime]:
    """Extract market resolution time from various possible field names."""
    for field_name in ("endDate", "resolutionTime", "end_date", "resolution_time"):
        val = raw.get(field_name)
        if val is None:
            continue
        try:
            if isinstance(val, str):
                return datetime.fromisoformat(val.replace("Z", "+00:00"))
            elif isinstance(val, (int, float)):
                ts = val / 1000.0 if val > 1e12 else float(val)
                return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, OSError):
            continue
    return None


def _parse_market(raw: dict) -> Market:
    tokens = raw.get("tokens", [])
    token_yes = ""
    token_no = ""
    for t in tokens:
        outcome = str(t.get("outcome", "")).lower()
        tid = str(t.get("token_id", t.get("tokenID", "")))
        if outcome == "yes":
            token_yes = tid
        elif outcome == "no":
            token_no = tid

    return Market(
        condition_id=str(raw.get("condition_id", raw.get("conditionId", raw.get("id", "")))),
        question=str(raw.get("question", raw.get("title", ""))),
        token_id_yes=token_yes or str(raw.get("token_id_yes", "")),
        token_id_no=token_no or str(raw.get("token_id_no", "")),
        resolve_time=_parse_resolve_time(raw),
        volume_24h=float(raw.get("volume24hr", raw.get("volume_24h", 0)) or 0),
        active=bool(raw.get("active", True)),
    )

"""Client for the Polymarket Gamma API (market discovery, no auth required)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp

from polymarket_copier.models.types import Market

logger = logging.getLogger("polymarket_copier")

GAMMA_API_BASE = "https://gamma-api.polymarket.com"
# Price-by-token-id is a CLOB concept, not a Gamma one — see get_market_price.
CLOB_API_BASE = "https://clob.polymarket.com"

# Connection pool sizing. The Gamma/CLOB clients sit on the hot detection→copy
# path (get_market + get_market_price fire concurrently per trade event, plus a
# per-tick midpoint poll), so reusing keep-alive connections avoids a fresh TLS
# handshake on every call — material latency when running on a local server.
_CONN_LIMIT = 20
_KEEPALIVE_TIMEOUT = 30


class GammaClient:
    """Wraps the Polymarket Gamma API for market and event discovery."""

    def __init__(self, base_url: str = GAMMA_API_BASE, session: Optional[aiohttp.ClientSession] = None):
        self.base_url = base_url.rstrip("/")
        self._external_session = session is not None
        self._session = session
        # Guards lazy session creation. Without it, two coroutines launched via
        # asyncio.gather (copier fires get_market + get_market_price together)
        # can both observe `_session is None` and each build a ClientSession —
        # one is orphaned and never closed ("Unclosed client session" + fd leak).
        self._session_lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        """Return the shared aiohttp session, lazily creating it under a lock to avoid orphaned sessions."""
        # Fast path: an open session already exists, no lock needed.
        if self._session is not None and not self._session.closed:
            return self._session
        async with self._session_lock:
            # Re-check inside the lock — a racing caller may have just built it.
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=10),
                    connector=aiohttp.TCPConnector(limit=_CONN_LIMIT, keepalive_timeout=_KEEPALIVE_TIMEOUT),
                )
        return self._session

    async def close(self) -> None:
        """Close the aiohttp session unless it was supplied externally by the caller."""
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
                # Use explicit None checks, not `a or b`: a legitimate midpoint of
                # 0.0 is falsy and would otherwise be skipped for the next key.
                raw = data.get("mid")
                if raw is None:
                    raw = data.get("midpoint")
                if raw is None:
                    raw = data.get("price")
                if raw is not None:
                    price = float(raw)
                    if not (0.0 <= price <= 1.0):
                        logger.warning(
                            "Rejecting out-of-range price %.6f for token %s (Polymarket tokens are bounded in [0, 1])",
                            price,
                            token_id[:10],
                        )
                        return None
                    return price
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

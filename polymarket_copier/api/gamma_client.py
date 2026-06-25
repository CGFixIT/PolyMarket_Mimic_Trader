"""Client for the Polymarket Gamma API (market discovery, no auth required)."""

from __future__ import annotations

import asyncio
import logging
import time
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

# M5: market metadata doesn't change mid-event — condition_id, tokens, resolve_time
# are stable once a market is active. Cache for 5 minutes to avoid re-fetching on
# every incoming trade event (a busy wallet can fire dozens of events per minute).
_MARKET_CACHE_TTL_SECONDS = 300


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
        # M5: TTL cache for market metadata. Keyed by condition_id; value is
        # (fetched_at_monotonic, Market). Entries expire after _MARKET_CACHE_TTL_SECONDS.
        self._market_cache: dict[str, tuple[float, Market]] = {}

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
        """Fetch a single market by condition ID or slug (M5: TTL-cached for 5 min)."""
        now = time.monotonic()
        cached = self._market_cache.get(condition_id)
        if cached is not None and (now - cached[0]) < _MARKET_CACHE_TTL_SECONDS:
            return cached[1]
        try:
            data = await self._get(f"/markets/{condition_id}")
            if isinstance(data, dict):
                market = _parse_market(data)
                self._market_cache[condition_id] = (now, market)
                return market
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
        event_id=_parse_event_id(raw),
        category=_parse_category(raw),
    )


def _parse_event_id(raw: dict) -> str:
    """Extract the parent-event identifier so correlated markets share an exposure bucket.

    Gamma nests markets under an ``events`` array; older/flatter payloads expose
    ``eventSlug``/``event_id`` directly. We prefer the first event's stable slug
    or id. Returns "" when no event grouping is present (cap then no-ops).
    """
    events = raw.get("events")
    if isinstance(events, list) and events:
        ev = events[0]
        if isinstance(ev, dict):
            for key in ("id", "slug", "ticker"):
                val = ev.get(key)
                if val:
                    return str(val)
    for key in ("event_id", "eventId", "eventSlug", "event_slug"):
        val = raw.get(key)
        if val:
            return str(val)
    return ""


def _parse_category(raw: dict) -> str:
    """Extract a coarse, lowercased market category for regime-aware TP/SL widths.

    Tries an explicit ``category`` field, then the first tag's label/slug. Returns
    "" when nothing usable is present so the vol multiplier defaults to 1.0x.
    """
    cat = raw.get("category")
    if cat:
        return str(cat).strip().lower()
    tags = raw.get("tags")
    if isinstance(tags, list) and tags:
        first = tags[0]
        if isinstance(first, dict):
            label = first.get("label") or first.get("slug") or first.get("name")
            if label:
                return str(label).strip().lower()
        elif isinstance(first, str) and first:
            return first.strip().lower()
    return ""

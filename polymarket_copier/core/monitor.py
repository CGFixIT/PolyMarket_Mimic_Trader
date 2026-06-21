"""
core/monitor.py — WebSocket-first trade monitor with polling fallback.

ARCHITECTURE DECISION — WHY TWO PATHS
--------------------------------------
The Polymarket CLOB WebSocket (wss://ws-subscriptions-clob.polymarket.com/ws/)
publishes real-time market events (price changes, order book updates) for
subscribed token IDs. However, the public market channel does NOT filter events
by wallet address — it broadcasts all price/trade activity for a market.

This creates a split responsibility:

  WebSocket path  — Track PRICE FEEDS for open positions we already hold.
                    Real-time, low latency.

  REST poll path  — Detect NEW TRADES from tracked wallets via the Data API.
                    Polymarket does not expose a wallet-filtered WebSocket event
                    stream in the public API. We must poll /activity per wallet.

TRADE EVENT FLOW
-----------------
1. REST poller detects new trade from tracked wallet → TradeEvent emitted
2. Copier receives TradeEvent, validates via RiskManager, places copy order
3. Position registered → token_id added to WebSocket subscription
4. WebSocket feeds real-time price → RiskManager.evaluate() on every tick
5. Exit condition hit → exit order placed, position de-registered from WS
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Dict, List, Optional, Set

import aiohttp

# websockets is an optional hard-dep; linter-safe import with graceful fallback
try:
    import websockets
    import websockets.exceptions  # noqa: F401
    _WS_AVAILABLE = True
except ImportError:
    _WS_AVAILABLE = False

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

POLYMARKET_WS_URL    = "wss://ws-subscriptions-clob.polymarket.com/ws/"
POLYMARKET_DATA_API  = "https://data-api.polymarket.com"

_WS_PING_INTERVAL    = 30     # seconds between WebSocket keep-alive pings
_WS_RECONNECT_DELAY  = 5      # seconds before reconnecting after WS drop
_POLL_INTERVAL_SEC   = 8      # seconds between wallet activity polls
_MAX_TRADES_PER_POLL = 50     # number of recent trades to fetch per poll cycle
_MAX_WS_RETRIES      = 5      # max consecutive WS reconnect attempts before fallback


# ─── Data Models ──────────────────────────────────────────────────────────────

class TradeType(Enum):
    BUY         = "BUY"    # New long position opened
    SELL        = "SELL"   # Position partially or fully exited
    SIZE_UP     = "SIZE_UP"
    SIZE_DOWN   = "SIZE_DOWN"


@dataclass(frozen=True)
class TradeEvent:
    """
    Emitted when a tracked wallet executes a trade.
    The copier module subscribes to these and decides whether to copy.
    """
    event_id:        str
    wallet_address:  str
    market_id:       str      # condition_id (market/question identifier)
    token_id:        str      # asset_id (YES or NO token)
    outcome_label:   str      # "Yes" or "No"
    trade_type:      TradeType
    price:           float    # Trade execution price, e.g. 0.72
    size_usdc:       float    # Trade size in USDC
    timestamp:       float    # Unix timestamp of the trade
    transaction_hash: str


@dataclass
class PriceTick:
    """Emitted by WebSocket for each real-time price update on a subscribed token."""
    token_id:  str
    price:     float
    timestamp: float = field(default_factory=time.time)


# Callback types — the copier's handlers are coroutine functions, so the
# callbacks return an awaitable that MUST be awaited at the call site.
TradeCallback = Callable[[TradeEvent], Awaitable[None]]
PriceCallback = Callable[[PriceTick], Awaitable[None]]


# ─── TradeMonitor ─────────────────────────────────────────────────────────────

class TradeMonitor:
    """
    Monitors a set of tracked wallet addresses for new trades.

    Usage:
        monitor = TradeMonitor(
            tracked_wallets=["0xABC...", "0xDEF..."],
            on_trade=copier.handle_trade_event,
            on_price=copier.handle_price_tick,
        )
        await monitor.run()   # Runs both WS listener and poll loop concurrently.
    """

    def __init__(
        self,
        tracked_wallets: List[str],
        on_trade:        TradeCallback,
        on_price:        Optional[PriceCallback] = None,
        poll_interval:   float = _POLL_INTERVAL_SEC,
        ws_url:          str   = POLYMARKET_WS_URL,
        data_api_base:   str   = POLYMARKET_DATA_API,
        prime_on_start:  bool  = True,
    ):
        if not tracked_wallets:
            raise ValueError("tracked_wallets must be non-empty.")

        self._wallets:      List[str]               = [w.lower() for w in tracked_wallets]
        self._on_trade:     TradeCallback           = on_trade
        self._on_price:     Optional[PriceCallback] = on_price
        self._poll_interval = poll_interval
        self._ws_url        = ws_url
        self._data_api_base = data_api_base

        # Track last-seen trade IDs per wallet to detect new trades.
        # OrderedDict (used as an insertion-ordered set) so overflow eviction is
        # FIFO — popping the OLDEST id, never an arbitrary recent one that could
        # then be re-detected and re-copied as a duplicate.
        self._seen_trade_ids: Dict[str, "OrderedDict[str, None]"] = {
            w: OrderedDict() for w in self._wallets
        }

        # Wallets whose first poll has been used to PRIME the seen-set (seed the
        # baseline without copying). Prevents a cold start from copying up to
        # _MAX_TRADES_PER_POLL historical trades per wallet on launch. When
        # prime_on_start is False, every wallet is pre-marked as primed so the
        # first poll acts immediately (used in tests / replay scenarios).
        self._primed_wallets: Set[str] = (
            set() if prime_on_start else set(self._wallets)
        )

        # Token IDs currently subscribed in the WebSocket
        self._subscribed_tokens: Set[str] = set()

        # Whether the WS is currently connected and healthy
        self._ws_healthy: bool    = False
        self._ws_retry_count: int = 0

        # asyncio task handles
        self._ws_task:   Optional[asyncio.Task] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    # ── Public API ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Start both the WebSocket price listener and the REST activity poller."""
        logger.info(
            "TradeMonitor starting | wallets=%d ws=%s",
            len(self._wallets), "available" if _WS_AVAILABLE else "NOT INSTALLED",
        )

        tasks = [
            asyncio.create_task(self._poll_loop(), name="activity-poller"),
        ]

        if _WS_AVAILABLE:
            tasks.append(
                asyncio.create_task(self._ws_loop(), name="ws-listener")
            )
        else:
            logger.warning(
                "websockets package not installed. Running poll-only mode. "
                "Install with: pip install websockets>=12.0"
            )

        self._poll_task = tasks[0]
        self._ws_task   = tasks[1] if len(tasks) > 1 else None

        await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self) -> None:
        """Signal both loops to exit cleanly."""
        logger.info("TradeMonitor stopping.")
        self._stop_event.set()
        for task in [self._ws_task, self._poll_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    def subscribe_token(self, token_id: str) -> None:
        """Register a token ID for real-time price feed via WebSocket."""
        self._subscribed_tokens.add(token_id)
        logger.debug("Queued WS subscription for token %s", token_id)

    def unsubscribe_token(self, token_id: str) -> None:
        """Remove a token from the real-time price feed (after position closed)."""
        self._subscribed_tokens.discard(token_id)

    @property
    def ws_healthy(self) -> bool:
        return self._ws_healthy

    # ── WebSocket Loop ────────────────────────────────────────────────────────

    async def _ws_loop(self) -> None:
        """
        Maintains a persistent WebSocket connection to the Polymarket CLOB.
        Reconnects automatically with exponential back-off up to _MAX_WS_RETRIES.
        """
        while not self._stop_event.is_set():
            try:
                await self._ws_connect_and_listen()
                self._ws_retry_count = 0  # Reset on clean disconnect
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._ws_healthy      = False
                self._ws_retry_count += 1

                if self._ws_retry_count >= _MAX_WS_RETRIES:
                    logger.error(
                        "WebSocket failed after %d retries (%s). "
                        "Falling back to poll-only mode.",
                        _MAX_WS_RETRIES, exc,
                    )
                    return  # Task exits; poll loop continues

                delay = _WS_RECONNECT_DELAY * (2 ** (self._ws_retry_count - 1))
                logger.warning(
                    "WebSocket disconnected (%s). Retry %d/%d in %.0fs.",
                    exc, self._ws_retry_count, _MAX_WS_RETRIES, delay,
                )
                await asyncio.sleep(delay)

    async def _ws_connect_and_listen(self) -> None:
        """Open a single WebSocket connection and process messages until disconnect."""
        async with websockets.connect(
            self._ws_url,
            ping_interval=_WS_PING_INTERVAL,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            self._ws_healthy = True
            logger.info("WebSocket connected: %s", self._ws_url)

            if self._subscribed_tokens:
                await self._ws_send_subscription(ws, list(self._subscribed_tokens))

            async for raw_msg in ws:
                if self._stop_event.is_set():
                    break

                await self._maybe_update_subscription(ws)

                try:
                    await self._handle_ws_message(raw_msg)
                except Exception as exc:
                    logger.warning("WS message parse error: %s | raw=%r", exc, raw_msg[:200])

    async def _ws_send_subscription(self, ws, token_ids: List[str]) -> None:
        """Send a Market subscription message to the Polymarket CLOB WebSocket."""
        sub_msg = json.dumps({
            "auth":      {},       # No auth needed for public market data
            "type":      "Market",
            "markets":   [],
            "assets_ids": token_ids,
        })
        await ws.send(sub_msg)
        logger.info("WS subscribed to %d token(s): %s", len(token_ids), token_ids[:3])

    async def _maybe_update_subscription(self, ws) -> None:
        """
        If self._subscribed_tokens has changed since last subscription message,
        send an updated subscription. Placeholder for a production asyncio.Queue diff.
        """
        pass

    async def _handle_ws_message(self, raw: str) -> None:
        """Parse a WebSocket message and emit PriceTick events for subscribed tokens."""
        events = json.loads(raw)
        if not isinstance(events, list):
            events = [events]

        for event in events:
            event_type = event.get("event_type", "")
            asset_id   = event.get("asset_id", "")

            if asset_id not in self._subscribed_tokens:
                continue

            if event_type in ("price_change", "last_trade_price"):
                try:
                    price = float(event.get("price", 0))
                except (TypeError, ValueError):
                    continue

                if not (0.0 <= price <= 1.0):
                    logger.debug("WS price out of range [0,1]: %.6f (ignoring)", price)
                    continue

                tick = PriceTick(token_id=asset_id, price=price)
                if self._on_price:
                    await self._on_price(tick)

    # ── REST Polling Loop ─────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Polls the Polymarket Data API for new trades from tracked wallets."""
        async with aiohttp.ClientSession(
            headers={"User-Agent": "polymarket-copier/1.0"},
            connector=aiohttp.TCPConnector(limit=20),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as session:
            while not self._stop_event.is_set():
                cycle_start = time.monotonic()

                await self._poll_all_wallets(session)

                elapsed = time.monotonic() - cycle_start
                sleep   = max(0.0, self._poll_interval - elapsed)
                logger.debug("Poll cycle %.2fs. Next in %.2fs.", elapsed, sleep)

                try:
                    await asyncio.wait_for(
                        asyncio.shield(self._stop_event.wait()), timeout=sleep
                    )
                    break   # stop_event was set
                except asyncio.TimeoutError:
                    pass    # Normal — sleep elapsed, continue polling

    async def _poll_all_wallets(self, session: aiohttp.ClientSession) -> None:
        """Fetch activity for all tracked wallets concurrently."""
        tasks = [
            self._poll_wallet(session, wallet)
            for wallet in self._wallets
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for wallet, result in zip(self._wallets, results):
            if isinstance(result, Exception):
                logger.warning("Poll failed for wallet %s: %s", wallet[:10], result)

    async def _poll_wallet(
        self,
        session: aiohttp.ClientSession,
        wallet:  str,
    ) -> None:
        """Fetch recent activity for a single wallet and emit TradeEvents for new trades."""
        url    = f"{self._data_api_base}/activity"
        params = {"user": wallet, "limit": _MAX_TRADES_PER_POLL}

        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                logger.warning(
                    "Data API returned %d for wallet %s", resp.status, wallet[:10]
                )
                return

            activity: List[dict] = await resp.json()

        # COLD-START GUARD: the very first poll for a wallet only seeds the
        # baseline of already-seen trades. Acting on it would copy a backlog of
        # stale historical trades the moment the bot starts.
        prime = wallet not in self._primed_wallets

        new_trades = self._filter_new_trades(wallet, activity, prime=prime)

        if prime:
            self._primed_wallets.add(wallet)
            logger.info(
                "Primed wallet %s baseline (%d trade(s) seen, none copied)",
                wallet[:10], len(self._seen_trade_ids[wallet]),
            )
            return

        if new_trades:
            logger.info(
                "Detected %d new trade(s) from wallet %s",
                len(new_trades), wallet[:10]
            )

        for raw_trade in new_trades:
            event = _parse_trade_event(wallet, raw_trade)
            if event is not None:
                await self._on_trade(event)

    def _filter_new_trades(
        self,
        wallet:   str,
        activity: List[dict],
        prime:    bool = False,
    ) -> List[dict]:
        """
        Return only trades not previously seen for this wallet.
        Updates self._seen_trade_ids in place. Caps memory growth.

        When ``prime`` is True the seen-set is still seeded from ``activity`` but
        an empty list is returned — used on a wallet's first poll so the existing
        backlog is recorded as the baseline rather than copied (cold-start guard).
        """
        seen       = self._seen_trade_ids[wallet]
        new_trades = []

        for item in activity:
            if item.get("type", "").lower() not in ("trade", "buy", "sell"):
                continue

            trade_id = str(item.get("id") or item.get("transactionHash", ""))
            if not trade_id or trade_id in seen:
                continue

            seen[trade_id] = None
            if not prime:
                new_trades.append(item)

        # FIFO eviction: drop the OLDEST ids first so a recently-seen id can
        # never be evicted and then re-detected as "new" on the next poll.
        while len(seen) > _MAX_TRADES_PER_POLL * 2:
            seen.popitem(last=False)

        return new_trades


# ─── Parser ───────────────────────────────────────────────────────────────────

def _parse_trade_event(wallet: str, raw: dict) -> Optional[TradeEvent]:
    """
    Convert a raw Data API activity record to a typed TradeEvent.
    Returns None if the record is missing required fields or is malformed.
    """
    try:
        trade_id   = str(raw.get("id") or raw.get("transactionHash", ""))
        market_id  = str(raw.get("market", raw.get("conditionId", "")))
        token_id   = str(raw.get("asset", raw.get("tokenId", "")))
        side_raw   = str(raw.get("side", "")).upper()
        price      = float(raw.get("price", 0))
        size       = float(raw.get("size", raw.get("usdcSize", 0)))
        outcome    = raw.get("outcomeLabel", "YES" if raw.get("outcomeIndex", 0) == 0 else "NO")

        ts_raw = raw.get("timestamp", raw.get("createdAt", ""))
        if isinstance(ts_raw, str):
            from datetime import datetime
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp()
        elif isinstance(ts_raw, (int, float)):
            ts = ts_raw / 1_000.0 if ts_raw > 1e12 else float(ts_raw)
        else:
            ts = time.time()

        if not market_id or not token_id or price <= 0 or size <= 0:
            return None

        trade_type = TradeType.BUY if side_raw == "BUY" else TradeType.SELL

        return TradeEvent(
            event_id        = trade_id,
            wallet_address  = wallet,
            market_id       = market_id,
            token_id        = token_id,
            outcome_label   = outcome,
            trade_type      = trade_type,
            price           = price,
            size_usdc       = size,
            timestamp       = ts,
            transaction_hash = str(raw.get("transactionHash", "")),
        )

    except (KeyError, ValueError, TypeError) as exc:
        logger.warning("Failed to parse trade event: %s | raw=%r", exc, str(raw)[:200])
        return None

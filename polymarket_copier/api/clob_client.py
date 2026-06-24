"""Client for the Polymarket CLOB API (order placement, requires authentication)."""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import logging
import math
import threading
import time
from typing import Any, Optional

from polymarket_copier.config import AppConfig
from polymarket_copier.models.types import Order

logger = logging.getLogger("polymarket_copier")

CLOB_BASE = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet

# M12: below this many unfilled shares, a retry isn't worth a second round-trip.
_MIN_RETRY_SHARES = 1.0


def _extract_live_fields(d: Any) -> tuple[Optional[str], Optional[float], Optional[float]]:
    """Best-effort (order_id, filled_size, avg_price) from a venue order dict (M12).

    Tolerant of the field-name variants py-clob-client may report. Returns Nones for
    a non-dict or missing fields — callers treat a missing fill as 'unknown'.
    """
    if not isinstance(d, dict):
        return None, None, None
    order_id = d.get("orderID") or d.get("order_id") or d.get("id")
    filled: Optional[float] = None
    for k in ("filled_size", "matched_amount", "size_matched"):
        v = d.get(k)
        if v is not None:
            filled = float(v)
            break
    avg: Optional[float] = None
    for k in ("avg_price", "price"):
        v = d.get(k)
        if v is not None:
            avg = float(v)
            break
    return order_id, filled, avg


class InsufficientLiquidityError(RuntimeError):
    """Raised when the order book lacks depth to fill an order within 1% of price."""


class ClobClient:
    """Wraps the Polymarket CLOB for order placement and management.

    In paper mode, orders are logged but not sent. In live mode, uses
    py-clob-client to sign and submit orders.

    C1 fix: all blocking py-clob-client calls (EIP-712 signing + requests POST)
    run in a dedicated ThreadPoolExecutor so they never stall the asyncio event
    loop. The 8 s poll budget and every open position's TP/SL evaluation continue
    uninterrupted while an order is in flight.
    """

    def __init__(self, config: AppConfig):
        self.config = config
        self.paper_mode = config.mode == "paper"
        self._client: Any = None
        # Threading lock protects the lazy-init guard against concurrent threads
        # in the executor both seeing _client=None and double-initialising.
        self._init_lock = threading.Lock()
        # Dedicated thread pool for blocking CLOB calls (signing + HTTP).
        # Not created in paper mode — there are no blocking calls there.
        self._executor: Optional[concurrent.futures.ThreadPoolExecutor] = (
            None
            if self.paper_mode
            else concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="clob-signer")
        )

    async def _run_blocking(self, fn) -> Any:
        """Run a zero-arg callable in the CLOB thread pool without blocking the loop."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn)

    async def close(self) -> None:
        """Shut down the signer thread pool (call on bot shutdown)."""
        if self._executor:
            self._executor.shutdown(wait=False)

    async def preload_credentials(self) -> None:
        """Eagerly warm up the live CLOB client in the background thread pool (L3).

        create_or_derive_api_creds() is a blocking call that signs a challenge
        request to derive the CLOB API key from the wallet's private key. It runs
        lazily on the first live order by default, adding ~200-500ms to copy
        latency for that first trade. Calling this at startup amortises the cost
        before any whale trade is detected. No-op in paper mode.
        """
        if not self.paper_mode:
            await self._run_blocking(self._init_live_client)

    def _init_live_client(self) -> None:
        """Lazily construct and authenticate the live py-clob-client (thread-safe, single-init)."""
        with self._init_lock:
            if self._client is not None:
                return
            if not self.config.private_key:
                raise ValueError("POLY_PRIVATE_KEY required for live trading")
            try:
                from py_clob_client.client import ClobClient as _ClobClient

                self._client = _ClobClient(
                    CLOB_BASE,
                    key=self.config.private_key,
                    chain_id=CHAIN_ID,
                )
                if self.config.api_key and self.config.api_secret:
                    self._client.set_api_creds(
                        type(
                            "Creds",
                            (),
                            {
                                "api_key": self.config.api_key,
                                "api_secret": self.config.api_secret,
                                "api_passphrase": self.config.api_passphrase,
                            },
                        )()
                    )
                else:
                    creds = self._client.create_or_derive_api_creds()
                    self._client.set_api_creds(creds)
                logger.info("Live CLOB client initialized")
            except ImportError:
                raise ImportError("py-clob-client required for live trading: pip install py-clob-client") from None

    async def get_order_book(self, token_id: str) -> dict[str, Any]:
        """Return the order book for a token (a synthetic book in paper mode)."""
        if self.paper_mode:
            return {
                "bids": [{"price": "0.50", "size": "10000"}],
                "asks": [{"price": "0.51", "size": "10000"}],
            }
        # C1: run in thread pool so the HTTP GET doesn't stall the event loop.
        return await self._run_blocking(functools.partial(self._get_order_book_sync, token_id))

    def _get_order_book_sync(self, token_id: str) -> dict[str, Any]:
        self._init_live_client()
        return self._client.get_order_book(token_id)

    def _size_multiplier(self, size_usdc: float) -> float:
        """M11: bounded sqrt-of-size impact multiplier (>= 1.0).

        Returns 1.0 at/below slippage_size_threshold_usdc (sub-threshold orders are
        unchanged), then grows as 1 + coeff*(sqrt(size/threshold) - 1), clamped to
        [1, slippage_size_max_mult]. coeff=0 disables scaling entirely.
        """
        ct = self.config.copy_trading
        thr = ct.slippage_size_threshold_usdc
        coeff = ct.slippage_size_coeff
        if coeff <= 0.0 or thr <= 0.0 or size_usdc <= thr:
            return 1.0
        mult = 1.0 + coeff * (math.sqrt(size_usdc / thr) - 1.0)
        return min(max(mult, 1.0), ct.slippage_size_max_mult)

    def _effective_slippage(self, size_usdc: float) -> float:
        """M11: live-exec slippage tolerance scaled up by order size (impact-aware)."""
        return self.config.copy_trading.max_live_slippage_pct * self._size_multiplier(size_usdc)

    def _check_liquidity(self, book: dict, price: float, size_usdc: float) -> None:
        """Ensure the ASK side can fill a BUY at a volume-weighted average price within
        max_live_slippage_pct of the order price. A market BUY lifts asks, so the ask
        side — not the bid side — determines fillability.

        M11: this is a VWAP-of-needed-depth check, not a naive sum of shares below a
        single max price. The old sum accepted a book with a thin top-of-ask level
        inside the cap but the bulk of depth above it — an order would 'pass' yet fill
        at a bad average. Walking the asks cheapest-first and rejecting when the VWAP
        for the needed shares exceeds the cap is intrinsically size-aware: a large
        order's deeper VWAP organically breaches the (base) cap.
        """
        slippage_cap = self.config.copy_trading.max_live_slippage_pct
        asks = book.get("asks", [])
        max_price = price * (1.0 + slippage_cap)
        needed_shares = size_usdc / max(price, 1e-6)

        filled = 0.0
        cost = 0.0
        for level in sorted(asks, key=lambda lvl: float(lvl.get("price", 0))):
            level_price = float(level.get("price", 0))
            level_size = float(level.get("size", 0))
            take = min(level_size, needed_shares - filled)
            if take <= 0.0:
                break
            filled += take
            cost += take * level_price

        if filled < needed_shares:
            raise InsufficientLiquidityError(
                f"Insufficient liquidity: need {needed_shares:.2f} shares, ask side only holds {filled:.2f} shares"
            )
        vwap = cost / filled if filled > 0 else float("inf")
        if vwap > max_price:
            raise InsufficientLiquidityError(
                f"VWAP {vwap:.4f} for {needed_shares:.2f} shares exceeds "
                f"{slippage_cap * 100:.1f}% cap above ${price:.4f}"
            )

    async def place_order(self, order: Order, slippage_override: Optional[float] = None) -> dict[str, Any]:
        """Place an order on the CLOB. Returns order details or paper-mode simulation.

        ``slippage_override`` (M12) lets a retry cross more of the book by pricing the
        exec at a wider cap than the size-derived default. The liquidity gate is still
        run at this wider cap so the depth guard is never bypassed.
        """
        # The depth check only applies to live trading against a real order book.
        # Paper mode has no real book, so it does not gate — otherwise a synthetic
        # book would systematically skip valid markets across the [0,1] price range.
        if not self.paper_mode and order.side == "BUY":
            book = await self.get_order_book(order.token_id)
            self._check_liquidity(book, order.price, order.size_usdc)

        if self.paper_mode:
            # Simulate realistic fill: apply half-spread slippage + taker fee so
            # paper PnL reflects live execution costs (not a zero-cost fill).
            # M11: scale the slippage component by order size (same impact law as
            # live) so paper PnL reflects the deeper book-walk of large orders. Fee
            # is linear and size-independent, so it is NOT scaled.
            slip = self.config.copy_trading.paper_fill_slippage_pct * self._size_multiplier(order.size_usdc)
            fee = self.config.copy_trading.paper_taker_fee_pct
            cost = slip + fee
            if order.side == "BUY":
                fill_price = min(order.price * (1.0 + cost), 1.0)
            else:
                fill_price = max(order.price * (1.0 - cost), 0.0)

            result = {
                "status": "PAPER",
                "market_id": order.market_id,
                "token_id": order.token_id,
                "side": order.side,
                "size_usdc": order.size_usdc,
                "price": order.price,
                "fill_price": fill_price,
            }
            logger.info(
                "[PAPER] Order: %s $%.2f @ %.4f (fill %.4f, %.1f%% slip+fee) on %s",
                order.side,
                order.size_usdc,
                order.price,
                fill_price,
                cost * 100,
                order.market_id,
            )
            return result

        # C1: run signing + HTTP POST in thread pool — never blocks the event loop.
        # C2: pass the order_type field through so GTC/FOK/FAK are honoured on live orders.
        #     Also price aggressively through the book (slippage cap) so FOK entries
        #     cross the spread and FAK exits hit the bid — not a resting mid-limit.
        # M11: size-aware slippage — a large order is priced to cross deeper into
        # the book (it has to), symmetric for BUY/SELL. The VWAP liquidity gate
        # above already rejected orders whose needed depth isn't there at the base
        # cap, so this just ensures a marginal large order actually fills.
        slippage_cap = slippage_override if slippage_override is not None else self._effective_slippage(order.size_usdc)
        if order.side == "BUY":
            exec_price = min(order.price * (1.0 + slippage_cap), 1.0)
        else:
            exec_price = max(order.price * (1.0 - slippage_cap), 0.0)

        size_shares = order.size_usdc / order.price if order.price > 0 else 0
        order_type_str = order.order_type  # capture for closure

        def _place_sync() -> Any:
            self._init_live_client()
            from py_clob_client.order_builder.constants import BUY as CLOB_BUY, SELL as CLOB_SELL

            side = CLOB_BUY if order.side == "BUY" else CLOB_SELL
            kwargs: dict[str, Any] = dict(
                token_id=order.token_id,
                price=exec_price,
                size=size_shares,
                side=side,
            )
            # Wire the order type through; graceful fallback if clob_types unavailable.
            try:
                from py_clob_client.clob_types import OrderType

                otype = {
                    "GTC": OrderType.GTC,
                    "FOK": OrderType.FOK,
                    "FAK": getattr(OrderType, "FAK", OrderType.FOK),
                    "GTD": getattr(OrderType, "GTD", OrderType.GTC),
                }.get(order_type_str, OrderType.GTC)
                kwargs["order_type"] = otype
            except ImportError:
                pass  # older py-clob-client: fall through without order_type
            return self._client.create_and_post_order(**kwargs)

        signed_order = await self._run_blocking(_place_sync)
        logger.info(
            "[LIVE] Order: %s %s $%.2f @ %.4f (exec %.4f) -> %s",
            order.order_type,
            order.side,
            order.size_usdc,
            order.price,
            exec_price,
            signed_order,
        )
        # M12: surface order_id + fill fields at the TOP level so _reconcile_fill can
        # read them (the old {"result": signed_order} nesting hid them, so live fills
        # silently fell through to a full-fill assumption). 'raw' keeps the original.
        order_id, filled, avg = _extract_live_fields(signed_order)
        return {
            "status": "LIVE",
            "order_id": order_id,
            "filled_size": filled,
            "avg_price": avg,
            "raw": signed_order,
        }

    async def get_order(self, order_id: Optional[str]) -> Optional[dict[str, Any]]:
        """M12: read an order's current status/fill (the retry-confirm step).

        Paper mode reports a full fill (no-op). Live mode reads the venue order and
        normalizes to {status, filled_size, avg_price}. Returns None on any error or
        a missing order_id — the caller MUST treat None as 'ambiguous, do not retry'.
        """
        if self.paper_mode:
            return {"status": "MATCHED", "filled_size": None, "avg_price": None}
        if not order_id:
            return None

        def _get_sync() -> Any:
            self._init_live_client()
            return self._client.get_order(order_id)

        try:
            raw = await self._run_blocking(_get_sync)
        except Exception as e:
            logger.error("get_order(%s) failed: %s", order_id, e)
            return None
        _, filled, avg = _extract_live_fields(raw if isinstance(raw, dict) else {})
        status = raw.get("status") if isinstance(raw, dict) else None
        return {"status": status, "filled_size": filled, "avg_price": avg}

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an order by id, returning True on success (always True in paper mode)."""
        if self.paper_mode:
            logger.info("[PAPER] Cancel: %s", order_id)
            return True

        # C1: cancellations are also blocking HTTP calls.
        def _cancel_sync() -> None:
            self._init_live_client()
            self._client.cancel(order_id)

        try:
            await self._run_blocking(_cancel_sync)
            return True
        except Exception as e:
            logger.error("Failed to cancel %s: %s", order_id, e)
            return False

    async def place_order_with_timeout(self, order: Order) -> dict[str, Any]:
        """M12: place a live order; for RESTING types (GTC/GTD) that don't fill within
        the timeout, cancel and retry ONCE for the unfilled remainder at a wider cap.

        FOK/FAK self-cancel at the venue and bypass this path; paper mode and a
        disabled timeout delegate straight to place_order (default behavior unchanged).

        DOUBLE-POSITION SAFETY: a retry runs ONLY after the first order is confirmed
        terminal (cancel succeeded AND get_order returned a concrete fill), and is
        sized to the UNFILLED remainder, so total filled can never exceed the intended
        size. Any ambiguity (cancel fails, confirm unavailable) degrades to NO retry.
        """
        ct = self.config.copy_trading
        # Fast paths: paper, self-cancelling order types, or feature disabled.
        if (
            self.paper_mode
            or order.order_type in ("FOK", "FAK")
            or ct.live_order_timeout_seconds <= 0
            or ct.live_order_max_retries <= 0
        ):
            return await self.place_order(order)

        intended_shares = order.size_usdc / order.price if order.price > 0 else 0.0
        result = await self.place_order(order)  # first attempt (resting)
        order_id = result.get("order_id")
        filled = float(result.get("filled_size") or 0.0)

        # Poll for the resting order to fill, up to the timeout (return early if it does).
        deadline = time.monotonic() + ct.live_order_timeout_seconds
        poll = min(1.0, max(ct.live_order_timeout_seconds / 4.0, 0.01))
        while filled < intended_shares and order_id and time.monotonic() < deadline:
            await asyncio.sleep(poll)
            confirm = await self.get_order(order_id)
            if confirm is None:
                break  # ambiguous → stop polling, fall through to no-retry
            cf = confirm.get("filled_size")
            if cf is not None:
                filled = float(cf)
                if confirm.get("avg_price") is not None:
                    result["avg_price"] = confirm["avg_price"]
        result["filled_size"] = filled

        if filled >= intended_shares or not order_id:
            return result  # filled, or nothing to cancel → done

        # Unfilled at deadline → cancel, confirm terminal, then retry the remainder once.
        if not await self.cancel_order(order_id):
            logger.warning("M12: cancel failed for %s — NOT retrying (ambiguous)", order_id)
            return result
        confirm = await self.get_order(order_id)
        if confirm is None or confirm.get("filled_size") is None:
            logger.warning("M12: post-cancel confirm unavailable for %s — NOT retrying", order_id)
            return result

        confirmed = float(confirm["filled_size"])
        # Guard float underflow: if confirmed rounds above intended due to machine
        # epsilon, remaining could be a tiny negative. Clamp before the shares check.
        remaining = max(0.0, intended_shares - confirmed)
        if remaining <= _MIN_RETRY_SHARES:
            result["filled_size"] = confirmed
            if confirm.get("avg_price") is not None:
                result["avg_price"] = confirm["avg_price"]
            return result

        logger.info(
            "M12: retrying unfilled remainder %.2f/%.2f shares at wider slippage %.1f%%",
            remaining,
            intended_shares,
            ct.live_retry_slippage_pct * 100,
        )
        retry_order = order.model_copy(update={"size_usdc": remaining * order.price})
        retry = await self.place_order(retry_order, slippage_override=ct.live_retry_slippage_pct)
        retry_filled = float(retry.get("filled_size") or 0.0)

        total_filled = confirmed + retry_filled
        avg1 = float(result.get("avg_price") or order.price)
        avg2 = float(retry.get("avg_price") or order.price)
        merged_avg = (confirmed * avg1 + retry_filled * avg2) / total_filled if total_filled > 0 else order.price
        return {
            "status": "LIVE",
            "order_id": retry.get("order_id"),
            "filled_size": total_filled,
            "avg_price": merged_avg,
            "raw": {"attempt1": result.get("raw"), "attempt2": retry.get("raw")},
        }

    async def get_balance(self) -> Optional[float]:
        """Return the available USDC balance (configured bankroll in paper mode, None on error)."""
        if self.paper_mode:
            return self.config.bankroll

        def _balance_sync() -> float:
            self._init_live_client()
            return float(self._client.get_balance())

        try:
            return await self._run_blocking(_balance_sync)
        except Exception as e:
            logger.error("Failed to get balance: %s", e)
            return None

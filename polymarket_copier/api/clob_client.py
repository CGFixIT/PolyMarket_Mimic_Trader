"""Client for the Polymarket CLOB API (order placement, requires authentication)."""

from __future__ import annotations

import logging
from typing import Any, Optional

from polymarket_copier.config import AppConfig
from polymarket_copier.models.types import Order

logger = logging.getLogger("polymarket_copier")

CLOB_BASE = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet


class InsufficientLiquidityError(RuntimeError):
    """Raised when the order book lacks depth to fill an order within 1% of price."""


class ClobClient:
    """Wraps the Polymarket CLOB for order placement and management.

    In paper mode, orders are logged but not sent. In live mode, uses
    py-clob-client to sign and submit orders.
    """

    def __init__(self, config: AppConfig):
        self.config = config
        self.paper_mode = config.mode == "paper"
        self._client: Any = None

    def _init_live_client(self) -> None:
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
                    type("Creds", (), {
                        "api_key": self.config.api_key,
                        "api_secret": self.config.api_secret,
                        "api_passphrase": self.config.api_passphrase,
                    })()
                )
            else:
                creds = self._client.create_or_derive_api_creds()
                self._client.set_api_creds(creds)
            logger.info("Live CLOB client initialized")
        except ImportError:
            raise ImportError(
                "py-clob-client required for live trading: pip install py-clob-client"
            )

    async def get_order_book(self, token_id: str) -> dict[str, Any]:
        if self.paper_mode:
            return {
                "bids": [{"price": "0.50", "size": "10000"}],
                "asks": [{"price": "0.51", "size": "10000"}],
            }
        self._init_live_client()
        return self._client.get_order_book(token_id)

    def _check_liquidity(self, book: dict, price: float, size_usdc: float) -> None:
        """Ensure the ASK side has enough resting depth to fill a BUY without walking
        the book more than 1% above the order price. A market BUY lifts asks, so the
        ask side — not the bid side — is what determines fillability.

        We compare available SHARES (not notional) against the shares we need to buy.
        Comparing notional was a bug: it would reject valid orders whose ask-side
        notional happened to be less than size_usdc despite having ample share depth.
        """
        asks          = book.get("asks", [])
        max_price     = price * 1.01
        needed_shares = size_usdc / max(price, 1e-6)
        avail_shares  = 0.0
        for level in asks:
            level_price = float(level.get("price", 0))
            level_size  = float(level.get("size", 0))
            if level_price <= max_price:
                avail_shares += level_size
        if avail_shares < needed_shares:
            raise InsufficientLiquidityError(
                f"Insufficient liquidity: need {needed_shares:.2f} shares, "
                f"available {avail_shares:.2f} shares on ask side within 1% of ${price:.4f}"
            )

    async def place_order(self, order: Order) -> dict[str, Any]:
        """Place an order on the CLOB. Returns order details or paper-mode simulation."""
        # The depth check only applies to live trading against a real order book.
        # Paper mode has no real book, so it does not gate — otherwise a synthetic
        # book would systematically skip valid markets across the [0,1] price range.
        if not self.paper_mode and order.side == "BUY":
            book = await self.get_order_book(order.token_id)
            self._check_liquidity(book, order.price, order.size_usdc)

        if self.paper_mode:
            # Simulate realistic fill: apply half-spread slippage + taker fee so
            # paper PnL reflects live execution costs (not a zero-cost fill).
            slip = self.config.copy_trading.paper_fill_slippage_pct
            fee  = self.config.copy_trading.paper_taker_fee_pct
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
                order.side, order.size_usdc, order.price, fill_price,
                cost * 100, order.market_id,
            )
            return result

        self._init_live_client()
        from py_clob_client.order_builder.constants import BUY as CLOB_BUY, SELL as CLOB_SELL

        side = CLOB_BUY if order.side == "BUY" else CLOB_SELL
        size_shares = order.size_usdc / order.price if order.price > 0 else 0
        signed_order = self._client.create_and_post_order(
            token_id=order.token_id,
            price=order.price,
            size=size_shares,
            side=side,
        )
        logger.info(
            "[LIVE] Order: %s $%.2f @ %.4f -> %s",
            order.side, order.size_usdc, order.price, signed_order,
        )
        return {"status": "LIVE", "result": signed_order}

    async def cancel_order(self, order_id: str) -> bool:
        if self.paper_mode:
            logger.info("[PAPER] Cancel: %s", order_id)
            return True
        self._init_live_client()
        try:
            self._client.cancel(order_id)
            return True
        except Exception as e:
            logger.error("Failed to cancel %s: %s", order_id, e)
            return False

    async def get_balance(self) -> Optional[float]:
        if self.paper_mode:
            return self.config.bankroll
        self._init_live_client()
        try:
            return float(self._client.get_balance())
        except Exception as e:
            logger.error("Failed to get balance: %s", e)
            return None

"""Client for the Polymarket CLOB API (order placement, requires authentication)."""

from __future__ import annotations

import logging
from typing import Any, Optional

from polymarket_copier.config import AppConfig
from polymarket_copier.models.types import CopyOrder, TradeSide

logger = logging.getLogger("polymarket_copier")

CLOB_BASE = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet


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
            if self.config.api_key:
                self._client.set_api_creds(
                    self._client.create_or_derive_api_creds()
                    if not self.config.api_secret
                    else type("Creds", (), {
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
            raise ImportError("py-clob-client is required for live trading: pip install py-clob-client")

    async def place_order(self, order: CopyOrder) -> dict[str, Any]:
        """Place an order on the CLOB. Returns order details or paper-mode simulation."""
        if self.paper_mode:
            result = {
                "status": "PAPER",
                "market_id": order.market_id,
                "asset_id": order.asset_id,
                "side": order.side.value,
                "size": order.size,
                "price": order.price,
            }
            logger.info("[PAPER] Order placed: %s %.4f @ %.4f on %s", order.side.value, order.size, order.price, order.market_id)
            return result

        self._init_live_client()
        from py_clob_client.order_builder.constants import BUY as CLOB_BUY, SELL as CLOB_SELL

        side = CLOB_BUY if order.side == TradeSide.BUY else CLOB_SELL
        signed_order = self._client.create_and_post_order(
            token_id=order.asset_id,
            price=order.price,
            size=order.size,
            side=side,
        )
        logger.info("[LIVE] Order placed: %s %.4f @ %.4f on %s -> %s", order.side.value, order.size, order.price, order.market_id, signed_order)
        return {"status": "LIVE", "result": signed_order}

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an existing order."""
        if self.paper_mode:
            logger.info("[PAPER] Order cancelled: %s", order_id)
            return True

        self._init_live_client()
        try:
            self._client.cancel(order_id)
            logger.info("[LIVE] Order cancelled: %s", order_id)
            return True
        except Exception as e:
            logger.error("Failed to cancel order %s: %s", order_id, e)
            return False

    async def get_balance(self) -> Optional[float]:
        """Get available USDC balance."""
        if self.paper_mode:
            return self.config.bankroll

        self._init_live_client()
        try:
            balance = self._client.get_balance()
            return float(balance) if balance else None
        except Exception as e:
            logger.error("Failed to get balance: %s", e)
            return None

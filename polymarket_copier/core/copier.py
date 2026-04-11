"""Copy trade engine — receives trade events and executes conservative copies."""

from __future__ import annotations

import logging
import uuid

from polymarket_copier.api.clob_client import ClobClient
from polymarket_copier.api.gamma_client import GammaClient
from polymarket_copier.core.portfolio import Portfolio
from polymarket_copier.core.risk_manager import RiskManager
from polymarket_copier.models.types import CopyOrder, ExitReason, Position, Trade, TradeSide

logger = logging.getLogger("polymarket_copier")


class CopyTrader:
    """Copies trades from tracked wallets with conservative risk parameters."""

    def __init__(
        self,
        clob_client: ClobClient,
        gamma_client: GammaClient,
        risk_manager: RiskManager,
        portfolio: Portfolio,
    ):
        self.clob = clob_client
        self.gamma = gamma_client
        self.risk = risk_manager
        self.portfolio = portfolio

    async def handle_trade(self, trade: Trade) -> None:
        """Process a new trade from a tracked wallet."""
        logger.info(
            "Processing trade from %s: %s %.4f @ %.4f on %s",
            trade.trader_address[:10],
            trade.side.value,
            trade.size,
            trade.price,
            trade.market_id[:10] if trade.market_id else "unknown",
        )

        if trade.side == TradeSide.SELL:
            logger.debug("Skipping SELL trade — we manage our own exits")
            return

        # Check if we can open a new position
        allowed, reason = self.risk.can_open_position(trade, self.portfolio.open_positions)
        if not allowed:
            logger.info("Trade blocked by risk manager: %s", reason)
            return

        # Check price deviation
        current_price = await self.gamma.get_market_price(trade.asset_id or trade.market_id)
        if current_price is None:
            current_price = trade.price

        if not self.risk.check_price_deviation(trade.price, current_price):
            deviation = abs(current_price - trade.price) / trade.price if trade.price else 0
            logger.info(
                "Trade skipped — price deviation too high: %.1f%% (max %.1f%%)",
                deviation * 100,
                self.risk.copy.max_price_deviation * 100,
            )
            return

        # Calculate conservative copy size
        copy_size = self.risk.calculate_copy_size(trade.size)
        if copy_size <= 0:
            logger.info("Trade skipped — calculated size is 0")
            return

        order = CopyOrder(
            market_id=trade.market_id,
            asset_id=trade.asset_id,
            side=trade.side,
            size=copy_size,
            price=current_price,
            source_trade=trade,
            source_trader=trade.trader_address,
        )

        result = await self.clob.place_order(order)

        position = Position(
            id=str(uuid.uuid4()),
            market_id=trade.market_id,
            asset_id=trade.asset_id,
            side=trade.side,
            size=copy_size,
            entry_price=current_price,
            current_price=current_price,
            peak_price=current_price,
            source_trader=trade.trader_address,
            market_slug=trade.market_slug,
            outcome=trade.outcome,
        )
        self.portfolio.add_position(position)

        logger.info(
            "Copy trade executed: %s %.4f @ %.4f (original: %.4f @ %.4f) status=%s",
            order.side.value,
            order.size,
            order.price,
            trade.size,
            trade.price,
            result.get("status", "unknown"),
        )

    async def check_exits(self) -> None:
        """Check all open positions against risk thresholds and exit if needed."""
        positions_to_close: list[tuple[str, ExitReason]] = []

        for position in self.portfolio.open_positions:
            # Update price
            price = await self.gamma.get_market_price(position.asset_id or position.market_id)
            if price is not None:
                position.update_price(price)

            exit_reason = self.risk.check_position_exit(position)
            if exit_reason:
                positions_to_close.append((position.id, exit_reason))

        for position_id, reason in positions_to_close:
            await self._close_position(position_id, reason)

    async def _close_position(self, position_id: str, reason: ExitReason) -> None:
        """Close a position by placing an exit order."""
        position = self.portfolio.positions.get(position_id)
        if position is None:
            return

        exit_side = TradeSide.SELL if position.side == TradeSide.BUY else TradeSide.BUY
        exit_order = CopyOrder(
            market_id=position.market_id,
            asset_id=position.asset_id,
            side=exit_side,
            size=position.size,
            price=position.current_price,
            source_trade=Trade(
                id=f"exit-{position_id}",
                trader_address=position.source_trader,
                market_id=position.market_id,
                asset_id=position.asset_id,
                side=exit_side,
                size=position.size,
                price=position.current_price,
            ),
            source_trader=position.source_trader,
        )

        await self.clob.place_order(exit_order)

        pnl = self.portfolio.close_position(position_id, position.current_price, reason)
        if pnl is not None:
            self.risk.record_trade_result(pnl, position.source_trader)

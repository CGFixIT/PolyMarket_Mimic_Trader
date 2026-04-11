"""Risk management — conservative sell thresholds and position limits."""

from __future__ import annotations

import logging
import time
from typing import Optional

from polymarket_copier.config import CopyTradingConfig, RiskManagementConfig
from polymarket_copier.models.types import CopyOrder, ExitReason, Position, Trade, TradeSide

logger = logging.getLogger("polymarket_copier")


class RiskManager:
    """Enforces conservative risk management rules on all trading decisions."""

    def __init__(self, risk_config: RiskManagementConfig, copy_config: CopyTradingConfig, bankroll: float):
        self.risk = risk_config
        self.copy = copy_config
        self.bankroll = bankroll
        self.daily_pnl: float = 0.0
        self.daily_reset_time: float = time.time()
        self.consecutive_losses: int = 0
        self.cooldown_until: float = 0.0
        self.trader_pnl: dict[str, float] = {}

    def reset_daily_if_needed(self) -> None:
        """Reset daily P&L counter at midnight."""
        elapsed = time.time() - self.daily_reset_time
        if elapsed >= 86400:
            logger.info("Daily P&L reset: was $%.2f", self.daily_pnl)
            self.daily_pnl = 0.0
            self.daily_reset_time = time.time()

    def check_position_exit(self, position: Position) -> Optional[ExitReason]:
        """Check if a position should be exited based on conservative thresholds."""
        pnl_pct = position.unrealized_pnl_pct

        # Take profit at 15%
        if pnl_pct >= self.risk.take_profit_pct:
            logger.info("TAKE_PROFIT triggered: %.1f%% >= %.1f%% on %s", pnl_pct * 100, self.risk.take_profit_pct * 100, position.market_id)
            return ExitReason.TAKE_PROFIT

        # Stop loss at 5%
        if pnl_pct <= -self.risk.stop_loss_pct:
            logger.info("STOP_LOSS triggered: %.1f%% <= -%.1f%% on %s", pnl_pct * 100, self.risk.stop_loss_pct * 100, position.market_id)
            return ExitReason.STOP_LOSS

        # Trailing stop: 10% drop from peak
        if position.peak_price > 0:
            drop = position.drop_from_peak_pct
            if drop >= self.risk.trailing_stop_pct:
                logger.info("TRAILING_STOP triggered: %.1f%% drop from peak on %s", drop * 100, position.market_id)
                return ExitReason.TRAILING_STOP

        # Time-based exit: 48h if flat (<3% move)
        if position.age_hours >= self.risk.time_exit_hours:
            if abs(pnl_pct) < self.risk.time_exit_min_move:
                logger.info("TIME_EXIT triggered: %.1fh old with only %.1f%% move on %s", position.age_hours, pnl_pct * 100, position.market_id)
                return ExitReason.TIME_EXIT

        return None

    def can_open_position(self, trade: Trade, open_positions: list[Position]) -> tuple[bool, str]:
        """Check if we're allowed to open a new position."""
        self.reset_daily_if_needed()

        # Daily loss limit
        if self.daily_pnl <= -(self.risk.daily_loss_limit_pct * self.bankroll):
            return False, f"Daily loss limit hit: ${self.daily_pnl:.2f}"

        # Cooldown after consecutive losses
        if time.time() < self.cooldown_until:
            remaining = (self.cooldown_until - time.time()) / 60
            return False, f"In cooldown: {remaining:.0f} min remaining"

        # Max concurrent positions
        if len(open_positions) >= self.copy.max_concurrent_positions:
            return False, f"Max positions ({self.copy.max_concurrent_positions}) reached"

        # Per-trader drawdown stop
        trader_addr = trade.trader_address
        trader_cumulative = self.trader_pnl.get(trader_addr, 0.0)
        if trader_cumulative <= -(self.risk.drawdown_stop_pct * self.bankroll):
            return False, f"Trader {trader_addr[:10]} drawdown stop: ${trader_cumulative:.2f}"

        # Per-trader allocation cap
        trader_exposure = sum(
            p.size * p.entry_price
            for p in open_positions
            if p.source_trader == trader_addr
        )
        max_allocation = self.copy.max_trader_allocation * self.bankroll
        if trader_exposure >= max_allocation:
            return False, f"Trader {trader_addr[:10]} allocation cap (${max_allocation:.2f}) reached"

        return True, ""

    def calculate_copy_size(self, original_size: float) -> float:
        """Calculate conservative copy size: min(0.5x original, 2% bankroll)."""
        scaled = original_size * self.copy.size_multiplier
        max_size = self.copy.max_trade_pct * self.bankroll
        return min(scaled, max_size)

    def check_price_deviation(self, trade_price: float, current_price: float) -> bool:
        """Return True if the current price is within acceptable deviation of the trade price."""
        if trade_price == 0:
            return False
        deviation = abs(current_price - trade_price) / trade_price
        return deviation <= self.copy.max_price_deviation

    def record_trade_result(self, pnl: float, trader_address: str) -> None:
        """Record the result of a closed trade for risk tracking."""
        self.daily_pnl += pnl
        self.trader_pnl[trader_address] = self.trader_pnl.get(trader_address, 0.0) + pnl

        if pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.risk.cooldown_after_losses:
                self.cooldown_until = time.time() + self.risk.cooldown_minutes * 60
                logger.warning(
                    "Cooldown activated: %d consecutive losses, pausing for %d min",
                    self.consecutive_losses,
                    self.risk.cooldown_minutes,
                )
        else:
            self.consecutive_losses = 0

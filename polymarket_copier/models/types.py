"""Data models for the Polymarket copy trading bot."""

from __future__ import annotations

import time
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class TradeSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class TradeAction(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    SIZE_CHANGE = "SIZE_CHANGE"


class ExitReason(str, Enum):
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    TRAILING_STOP = "TRAILING_STOP"
    TIME_EXIT = "TIME_EXIT"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    DRAWDOWN_STOP = "DRAWDOWN_STOP"
    MANUAL = "MANUAL"


class Trader(BaseModel):
    """A tracked trader from the leaderboard."""

    address: str
    pnl: float = 0.0
    win_rate: float = 0.0
    total_trades: int = 0
    volume: float = 0.0
    score: float = 0.0

    def compute_score(self, pnl_weight: float = 0.4, wr_weight: float = 0.35, consistency_weight: float = 0.25) -> float:
        pnl_norm = min(self.pnl / 100_000, 1.0) if self.pnl > 0 else 0.0
        wr_norm = self.win_rate
        consistency_norm = min(self.total_trades / 500, 1.0)
        self.score = pnl_norm * pnl_weight + wr_norm * wr_weight + consistency_norm * consistency_weight
        return self.score


class Trade(BaseModel):
    """A single trade event from a tracked trader."""

    id: str
    trader_address: str
    market_id: str
    asset_id: str
    side: TradeSide
    size: float
    price: float
    timestamp: float = Field(default_factory=time.time)
    market_slug: str = ""
    outcome: str = ""


class Position(BaseModel):
    """An open position held by the copy bot."""

    id: str
    market_id: str
    asset_id: str
    side: TradeSide
    size: float
    entry_price: float
    current_price: float = 0.0
    peak_price: float = 0.0
    entry_time: float = Field(default_factory=time.time)
    source_trader: str = ""
    market_slug: str = ""
    outcome: str = ""

    @property
    def unrealized_pnl_pct(self) -> float:
        if self.entry_price == 0:
            return 0.0
        if self.side == TradeSide.BUY:
            return (self.current_price - self.entry_price) / self.entry_price
        else:
            return (self.entry_price - self.current_price) / self.entry_price

    @property
    def unrealized_pnl(self) -> float:
        if self.side == TradeSide.BUY:
            return (self.current_price - self.entry_price) * self.size
        else:
            return (self.entry_price - self.current_price) * self.size

    @property
    def age_hours(self) -> float:
        return (time.time() - self.entry_time) / 3600

    def update_price(self, price: float) -> None:
        self.current_price = price
        if self.side == TradeSide.BUY and price > self.peak_price:
            self.peak_price = price
        elif self.side == TradeSide.SELL and (price < self.peak_price or self.peak_price == 0):
            self.peak_price = price

    @property
    def drop_from_peak_pct(self) -> float:
        if self.peak_price == 0 or self.entry_price == 0:
            return 0.0
        if self.side == TradeSide.BUY:
            return (self.peak_price - self.current_price) / self.peak_price
        else:
            if self.peak_price == 0:
                return 0.0
            return (self.current_price - self.peak_price) / self.peak_price


class CopyOrder(BaseModel):
    """An order to be placed by the copy bot."""

    market_id: str
    asset_id: str
    side: TradeSide
    size: float
    price: float
    source_trade: Trade
    source_trader: str = ""


class TradeRecord(BaseModel):
    """Record of an executed copy trade for logging."""

    id: str = ""
    order: CopyOrder
    executed: bool = False
    paper: bool = True
    timestamp: float = Field(default_factory=time.time)
    pnl: Optional[float] = None
    exit_reason: Optional[ExitReason] = None

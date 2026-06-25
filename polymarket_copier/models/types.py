"""Data models for the Polymarket copy trading bot v2."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class Market(BaseModel):
    """A Polymarket prediction market."""

    condition_id: str
    question: str = ""
    token_id_yes: str = ""
    token_id_no: str = ""
    resolve_time: Optional[datetime] = None
    volume_24h: float = 0.0
    active: bool = True
    # M7: identifier of the parent event grouping correlated markets (e.g. all
    # outcome markets of one election). Empty string falls back to per-market
    # accounting so the event cap is a no-op when the API omits event data.
    event_id: str = ""
    # M6: coarse market category (e.g. "politics", "crypto", "sports") used to
    # select a per-regime TP/SL width multiplier. Empty string → default 1.0x.
    category: str = ""


class Order(BaseModel):
    """An order to place on the CLOB."""

    market_id: str
    token_id: str
    side: Literal["BUY", "SELL"]
    price: float = Field(ge=0.0, le=1.0)
    size_usdc: float = Field(gt=0.0)
    order_type: Literal["GTC", "FOK", "GTD", "FAK"] = "GTC"

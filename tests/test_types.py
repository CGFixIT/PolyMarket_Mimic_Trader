"""Tests for the v2 Pydantic data models (Market, Order)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from polymarket_copier.models.types import Market, Order


class TestMarket:
    def test_minimal(self):
        m = Market(condition_id="c1")
        assert m.condition_id == "c1"
        assert m.active is True
        assert m.volume_24h == 0.0
        assert m.resolve_time is None

    def test_full(self):
        rt = datetime(2026, 12, 31, tzinfo=timezone.utc)
        m = Market(
            condition_id="c1",
            question="Will X happen?",
            token_id_yes="yes",
            token_id_no="no",
            resolve_time=rt,
            volume_24h=12345.0,
            active=True,
        )
        assert m.token_id_yes == "yes"
        assert m.resolve_time == rt


class TestOrder:
    def test_valid_buy(self):
        o = Order(market_id="m", token_id="t", side="BUY", price=0.65, size_usdc=100.0)
        assert o.side == "BUY"
        assert o.order_type == "GTC"

    def test_price_out_of_range_rejected(self):
        with pytest.raises(ValidationError):
            Order(market_id="m", token_id="t", side="BUY", price=1.5, size_usdc=100.0)

    def test_negative_price_rejected(self):
        with pytest.raises(ValidationError):
            Order(market_id="m", token_id="t", side="SELL", price=-0.1, size_usdc=100.0)

    def test_zero_size_rejected(self):
        with pytest.raises(ValidationError):
            Order(market_id="m", token_id="t", side="BUY", price=0.5, size_usdc=0.0)

    def test_invalid_side_rejected(self):
        with pytest.raises(ValidationError):
            Order(market_id="m", token_id="t", side="HOLD", price=0.5, size_usdc=10.0)

    def test_custom_order_type(self):
        o = Order(market_id="m", token_id="t", side="BUY", price=0.5, size_usdc=10.0, order_type="FOK")
        assert o.order_type == "FOK"

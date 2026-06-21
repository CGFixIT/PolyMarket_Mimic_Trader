"""Tests for the CLOB client — paper-mode behaviour and the live liquidity guard.

ClobClient is the only component that talks to the order book, so its
liquidity depth check (`_check_liquidity`) is the last line of defence against
submitting an order that cannot be filled near the intended price. These tests
exercise that guard plus the paper-mode short-circuits, without requiring the
optional `py-clob-client` dependency or any network access.
"""

from __future__ import annotations

import pytest

from polymarket_copier.api.clob_client import ClobClient, InsufficientLiquidityError
from polymarket_copier.config import AppConfig
from polymarket_copier.models.types import Order


@pytest.fixture
def paper_client() -> ClobClient:
    return ClobClient(AppConfig(mode="paper", bankroll=10_000))


@pytest.fixture
def live_client() -> ClobClient:
    # mode="live" but no private key: enough to exercise the pre-auth liquidity
    # path (which runs before any client init) without touching py-clob-client.
    return ClobClient(AppConfig(mode="live", bankroll=10_000))


def buy_order(price=0.50, size_usdc=100.0) -> Order:
    return Order(
        market_id="mkt-a", token_id="tok-a", side="BUY",
        price=price, size_usdc=size_usdc,
    )


class TestPaperMode:
    @pytest.mark.asyncio
    async def test_place_order_returns_paper_status(self, paper_client):
        result = await paper_client.place_order(buy_order())
        assert result["status"] == "PAPER"
        assert result["side"] == "BUY"
        assert result["size_usdc"] == 100.0

    @pytest.mark.asyncio
    async def test_get_order_book_is_synthetic(self, paper_client):
        book = await paper_client.get_order_book("tok-a")
        assert "bids" in book and "asks" in book

    @pytest.mark.asyncio
    async def test_paper_order_skips_liquidity_check(self, paper_client):
        # A huge paper BUY must NOT raise — the synthetic book is not a real
        # constraint and paper mode is documented to bypass the depth gate.
        result = await paper_client.place_order(buy_order(size_usdc=1_000_000))
        assert result["status"] == "PAPER"

    @pytest.mark.asyncio
    async def test_cancel_order_paper(self, paper_client):
        assert await paper_client.cancel_order("order-123") is True

    @pytest.mark.asyncio
    async def test_get_balance_returns_bankroll(self, paper_client):
        assert await paper_client.get_balance() == 10_000


class TestLiquidityGuard:
    def test_sufficient_depth_passes(self, paper_client):
        book = {"asks": [{"price": "0.50", "size": "1000"}]}  # $500 available
        # Should not raise: $500 >= $100 within 1% of 0.50.
        paper_client._check_liquidity(book, price=0.50, size_usdc=100.0)

    def test_insufficient_depth_raises(self, paper_client):
        book = {"asks": [{"price": "0.50", "size": "10"}]}  # only $5 available
        with pytest.raises(InsufficientLiquidityError, match="Insufficient liquidity"):
            paper_client._check_liquidity(book, price=0.50, size_usdc=100.0)

    def test_depth_outside_1pct_excluded(self, paper_client):
        # Only the 0.50 level (within 1% of 0.50 → max 0.505) counts; the deep
        # 0.60 level is too far above and must not be credited toward fillability.
        book = {"asks": [
            {"price": "0.50", "size": "100"},     # within band → $50
            {"price": "0.60", "size": "100000"},  # outside band → ignored
        ]}
        with pytest.raises(InsufficientLiquidityError):
            paper_client._check_liquidity(book, price=0.50, size_usdc=100.0)

    def test_empty_book_raises(self, paper_client):
        with pytest.raises(InsufficientLiquidityError):
            paper_client._check_liquidity({}, price=0.50, size_usdc=1.0)


class TestLiveModeGuards:
    def test_init_live_client_without_key_raises(self, live_client):
        with pytest.raises(ValueError, match="POLY_PRIVATE_KEY"):
            live_client._init_live_client()

    @pytest.mark.asyncio
    async def test_live_buy_thin_book_raises_before_auth(self, live_client):
        # In live mode a BUY triggers the depth check first. With a thin book the
        # InsufficientLiquidityError must surface BEFORE any auth/client init, so
        # this works even without a private key or py-clob-client installed.
        async def thin_book(_token_id):
            return {"asks": [{"price": "0.50", "size": "1"}], "bids": []}

        live_client.get_order_book = thin_book
        with pytest.raises(InsufficientLiquidityError):
            await live_client.place_order(buy_order(size_usdc=100.0))

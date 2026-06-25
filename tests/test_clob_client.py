"""Tests for the CLOB client — paper-mode behaviour and the live liquidity guard.

ClobClient is the only component that talks to the order book, so its
liquidity depth check (`_check_liquidity`) is the last line of defence against
submitting an order that cannot be filled near the intended price. These tests
exercise that guard plus the paper-mode short-circuits, without requiring the
optional `py-clob-client` dependency or any network access.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from polymarket_copier.api.clob_client import (
    ClobClient,
    InsufficientLiquidityError,
    _extract_live_fields,
)
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


def buy_order(price=0.50, size_usdc=100.0, order_type="FOK") -> Order:
    return Order(
        market_id="mkt-a",
        token_id="tok-a",
        side="BUY",
        price=price,
        size_usdc=size_usdc,
        order_type=order_type,
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
        book = {
            "asks": [
                {"price": "0.50", "size": "100"},  # within band → $50
                {"price": "0.60", "size": "100000"},  # outside band → ignored
            ]
        }
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


# ─── Realistic paper fill price ───────────────────────────────────────────────


class TestPaperFillPrice:
    """Paper mode now returns a slippage+fee-adjusted fill_price so paper PnL
    reflects live execution costs rather than a zero-cost optimistic fill."""

    @pytest.mark.asyncio
    async def test_buy_fill_price_above_order_price(self, paper_client):
        result = await paper_client.place_order(buy_order(price=0.50))
        assert "fill_price" in result
        # fill = 0.50 * (1 + 0.005 + 0.02) = 0.5125
        assert result["fill_price"] == pytest.approx(0.5125)

    @pytest.mark.asyncio
    async def test_sell_fill_price_below_order_price(self, paper_client):
        order = Order(market_id="mkt-a", token_id="tok-a", side="SELL", price=0.80, size_usdc=100.0, order_type="FAK")
        result = await paper_client.place_order(order)
        assert "fill_price" in result
        # fill = 0.80 * (1 - 0.025) = 0.78
        assert result["fill_price"] == pytest.approx(0.80 * 0.975)

    @pytest.mark.asyncio
    async def test_fill_price_clamped_to_one_on_buy(self, paper_client):
        # Buying near the ceiling: fill price must not exceed 1.0
        result = await paper_client.place_order(buy_order(price=0.99))
        assert result["fill_price"] <= 1.0

    @pytest.mark.asyncio
    async def test_fill_price_floored_to_zero_on_sell(self, paper_client):
        order = Order(market_id="mkt-a", token_id="tok-a", side="SELL", price=0.01, size_usdc=1.0)
        result = await paper_client.place_order(order)
        assert result["fill_price"] >= 0.0

    @pytest.mark.asyncio
    async def test_custom_slippage_fee_respected(self):
        from polymarket_copier.config import AppConfig

        cfg = AppConfig(mode="paper", bankroll=10_000)
        cfg.copy_trading.paper_fill_slippage_pct = 0.01
        cfg.copy_trading.paper_taker_fee_pct = 0.03
        client = ClobClient(cfg)
        result = await client.place_order(buy_order(price=0.50))
        # fill = 0.50 * (1 + 0.01 + 0.03) = 0.52
        assert result["fill_price"] == pytest.approx(0.52)


# ─── Live slippage cap (configurable) ────────────────────────────────────────


class TestLiveSlippageCap:
    """max_live_slippage_pct replaces the formerly hardcoded 1% band in
    _check_liquidity, allowing operators to tighten or widen the acceptable
    fill-price band without changing code."""

    def test_default_cap_is_1pct(self, paper_client):
        """Default max_live_slippage_pct=0.01 → same behaviour as the old 1% hardcode."""
        book = {
            "asks": [
                {"price": "0.505", "size": "500"},  # 1% above $0.50 → exactly at cap
            ]
        }
        # Should not raise: ask is within 1% of 0.50, enough depth.
        paper_client._check_liquidity(book, price=0.50, size_usdc=100.0)

    def test_wider_cap_admits_far_asks(self, paper_client):
        """Widening the cap to 5% allows asks up to 0.525 to count as fillable."""
        paper_client.config.copy_trading.max_live_slippage_pct = 0.05
        book = {
            "asks": [
                {"price": "0.52", "size": "500"},  # 4% above 0.50 — excluded at 1%, ok at 5%
            ]
        }
        paper_client._check_liquidity(book, price=0.50, size_usdc=100.0)

    def test_tighter_cap_excludes_borderline_asks(self, paper_client):
        """Tightening the cap to 0% means even a 0.001 tick above price is excluded."""
        paper_client.config.copy_trading.max_live_slippage_pct = 0.0
        book = {
            "asks": [
                {"price": "0.501", "size": "10000"},  # just above → excluded with 0% cap
            ]
        }
        with pytest.raises(InsufficientLiquidityError, match="0.0%"):
            paper_client._check_liquidity(book, price=0.50, size_usdc=100.0)

    def test_error_message_includes_configured_pct(self, paper_client):
        """The VWAP-exceeds-cap error must show the configured cap, not a hardcoded 1%."""
        paper_client.config.copy_trading.max_live_slippage_pct = 0.03
        # Ample depth, but priced 20% above → VWAP breaches the 3% cap.
        book = {"asks": [{"price": "0.60", "size": "500"}]}
        with pytest.raises(InsufficientLiquidityError, match="3.0%"):
            paper_client._check_liquidity(book, price=0.50, size_usdc=100.0)

    def test_insufficient_total_depth_raises(self, paper_client):
        """Too few total shares on the ask side → raise (regardless of price)."""
        book = {"asks": [{"price": "0.50", "size": "1"}]}  # only 1 share, need 200
        with pytest.raises(InsufficientLiquidityError, match="only holds"):
            paper_client._check_liquidity(book, price=0.50, size_usdc=100.0)

    def test_vwap_rejects_thin_top_deep_book(self, paper_client):
        """M11: a thin top-of-ask inside the cap plus the bulk of depth ABOVE it must
        be rejected — the old sum-of-shares-below-max-price check missed this."""
        paper_client.config.copy_trading.max_live_slippage_pct = 0.01  # max_price 0.505
        book = {
            "asks": [
                {"price": "0.505", "size": "5"},  # tiny slice inside the cap
                {"price": "0.70", "size": "1000"},  # the real depth, far above the cap
            ]
        }
        # need 200 shares; VWAP = (5*0.505 + 195*0.70)/200 ≈ 0.695 >> 0.505 → reject.
        with pytest.raises(InsufficientLiquidityError, match="VWAP"):
            paper_client._check_liquidity(book, price=0.50, size_usdc=100.0)

    def test_vwap_small_order_fills_at_top(self, paper_client):
        """A small order fillable entirely at the top ask passes the VWAP gate."""
        book = {
            "asks": [
                {"price": "0.501", "size": "1000"},  # within 1% cap, ample depth
                {"price": "0.90", "size": "1000"},
            ]
        }
        paper_client._check_liquidity(book, price=0.50, size_usdc=100.0)  # need 200, all @0.501


class TestSizeAwareSlippage:
    """M11: _size_multiplier / _effective_slippage scale slippage up with order size."""

    def test_below_threshold_is_unity(self, paper_client):
        paper_client.config.copy_trading.slippage_size_threshold_usdc = 500.0
        paper_client.config.copy_trading.slippage_size_coeff = 0.5
        assert paper_client._size_multiplier(100.0) == 1.0
        assert paper_client._size_multiplier(500.0) == 1.0  # at threshold, still unity

    def test_scales_sqrt_above_threshold(self, paper_client):
        paper_client.config.copy_trading.slippage_size_threshold_usdc = 500.0
        paper_client.config.copy_trading.slippage_size_coeff = 0.5
        paper_client.config.copy_trading.slippage_size_max_mult = 3.0
        # size = 4*threshold → sqrt(4)=2 → 1 + 0.5*(2-1) = 1.5
        assert paper_client._size_multiplier(2000.0) == pytest.approx(1.5)

    def test_capped_at_max_mult(self, paper_client):
        paper_client.config.copy_trading.slippage_size_threshold_usdc = 500.0
        paper_client.config.copy_trading.slippage_size_coeff = 0.5
        paper_client.config.copy_trading.slippage_size_max_mult = 3.0
        assert paper_client._size_multiplier(1_000_000.0) == 3.0

    def test_coeff_zero_disables(self, paper_client):
        paper_client.config.copy_trading.slippage_size_coeff = 0.0
        assert paper_client._size_multiplier(1_000_000.0) == 1.0

    def test_effective_slippage_scales_base(self, paper_client):
        paper_client.config.copy_trading.max_live_slippage_pct = 0.01
        paper_client.config.copy_trading.slippage_size_threshold_usdc = 500.0
        paper_client.config.copy_trading.slippage_size_coeff = 0.5
        assert paper_client._effective_slippage(100.0) == pytest.approx(0.01)  # base
        assert paper_client._effective_slippage(2000.0) == pytest.approx(0.015)  # 1.5x

    @pytest.mark.asyncio
    async def test_paper_large_order_costs_more(self, paper_client):
        """A >threshold paper order fills at a worse price than a small one (scaled slip)."""
        paper_client.config.copy_trading.paper_fill_slippage_pct = 0.005
        paper_client.config.copy_trading.paper_taker_fee_pct = 0.02
        paper_client.config.copy_trading.slippage_size_threshold_usdc = 500.0
        paper_client.config.copy_trading.slippage_size_coeff = 0.5
        small = await paper_client.place_order(buy_order(price=0.50, size_usdc=100.0))
        large = await paper_client.place_order(buy_order(price=0.50, size_usdc=4500.0))
        assert large["fill_price"] > small["fill_price"]


def _gtc_order(price=0.50, size_usdc=100.0) -> Order:
    return Order(market_id="mkt-a", token_id="tok-a", side="BUY", price=price, size_usdc=size_usdc, order_type="GTC")


def _orchestrator_client(timeout=0.05) -> ClobClient:
    """A client forced into the live orchestrator path (paper_mode off) with the
    venue methods mocked, so the M12 state machine runs without any network."""
    c = ClobClient(AppConfig(mode="paper", bankroll=10_000))
    c.paper_mode = False  # force place_order_with_timeout's resting-order branch
    c.config.copy_trading.live_order_timeout_seconds = timeout
    c.config.copy_trading.live_retry_slippage_pct = 0.02
    c.config.copy_trading.max_live_slippage_pct = 0.01
    c.config.copy_trading.live_order_max_retries = 1
    return c


class TestTickRounding:
    """M15: _round_exec_to_tick snaps the submitted price to the venue tick grid."""

    def test_buy_rounds_up_to_tick(self, paper_client):
        paper_client.config.copy_trading.order_price_tick = 0.01
        # 0.3774 (0.37 after 2% slippage) must round UP so the BUY still crosses.
        assert paper_client._round_exec_to_tick(0.3774, "BUY") == pytest.approx(0.38)

    def test_sell_rounds_down_to_tick(self, paper_client):
        paper_client.config.copy_trading.order_price_tick = 0.01
        # 0.3626 (0.37 after -2% slippage) must round DOWN so the SELL still hits the bid.
        assert paper_client._round_exec_to_tick(0.3626, "SELL") == pytest.approx(0.36)

    def test_already_on_tick_unchanged(self, paper_client):
        paper_client.config.copy_trading.order_price_tick = 0.01
        assert paper_client._round_exec_to_tick(0.42, "BUY") == pytest.approx(0.42)
        assert paper_client._round_exec_to_tick(0.42, "SELL") == pytest.approx(0.42)

    def test_finer_tick_grid(self, paper_client):
        paper_client.config.copy_trading.order_price_tick = 0.001
        assert paper_client._round_exec_to_tick(0.37745, "BUY") == pytest.approx(0.378)
        assert paper_client._round_exec_to_tick(0.37745, "SELL") == pytest.approx(0.377)

    def test_clamped_inside_unit_interval(self, paper_client):
        paper_client.config.copy_trading.order_price_tick = 0.01
        # A BUY rounding past 1.0 is pulled back to the last tradeable tick (0.99).
        assert paper_client._round_exec_to_tick(0.999, "BUY") == pytest.approx(0.99)
        # A SELL rounding to 0.0 is pulled up to the first tradeable tick (0.01).
        assert paper_client._round_exec_to_tick(0.004, "SELL") == pytest.approx(0.01)

    def test_zero_tick_disables_rounding(self, paper_client):
        paper_client.config.copy_trading.order_price_tick = 0.0
        assert paper_client._round_exec_to_tick(0.3774, "BUY") == pytest.approx(0.3774)

    def test_no_subtick_float_noise(self, paper_client):
        # Float accumulation (0.01 * 38) can leave noise like 0.38000000000000006;
        # the helper quantizes it so the venue sees a clean on-tick value.
        paper_client.config.copy_trading.order_price_tick = 0.01
        result = paper_client._round_exec_to_tick(0.3774, "BUY")
        assert result == round(result, 10)


class TestBookDepthUsdc:
    """M2: book_depth_usdc sums ask-side USDC fillable within the slippage cap."""

    @pytest.mark.asyncio
    async def test_sums_asks_within_cap(self, paper_client):
        paper_client.config.copy_trading.max_live_slippage_pct = 0.05  # 5% → max 0.525 at 0.50
        paper_client.get_order_book = AsyncMock(
            return_value={
                "asks": [
                    {"price": "0.50", "size": "100"},  # 0.50 <= 0.525 → 50 USDC
                    {"price": "0.52", "size": "200"},  # 0.52 <= 0.525 → 104 USDC
                    {"price": "0.60", "size": "999"},  # 0.60 > 0.525 → excluded
                ]
            }
        )
        depth = await paper_client.book_depth_usdc("tok", 0.50)
        assert depth == pytest.approx(0.50 * 100 + 0.52 * 200)

    @pytest.mark.asyncio
    async def test_empty_book_is_zero(self, paper_client):
        paper_client.get_order_book = AsyncMock(return_value={"asks": []})
        assert await paper_client.book_depth_usdc("tok", 0.50) == 0.0

    @pytest.mark.asyncio
    async def test_all_asks_outside_cap_is_zero(self, paper_client):
        paper_client.config.copy_trading.max_live_slippage_pct = 0.01
        paper_client.get_order_book = AsyncMock(return_value={"asks": [{"price": "0.60", "size": "100"}]})
        assert await paper_client.book_depth_usdc("tok", 0.50) == 0.0


class TestExtractLiveFields:
    def test_extracts_variants(self):
        oid, filled, avg = _extract_live_fields({"orderID": "o1", "matched_amount": "40", "price": "0.51"})
        assert oid == "o1" and filled == 40.0 and avg == 0.51

    def test_missing_fields_are_none(self):
        assert _extract_live_fields({}) == (None, None, None)
        assert _extract_live_fields("not-a-dict") == (None, None, None)


class TestPlaceOrderWithTimeout:
    """M12: cancel + confirm + retry-remainder-once for resting live orders, with
    hard double-position safety. FOK/FAK/paper bypass the path entirely."""

    @pytest.mark.asyncio
    async def test_paper_delegates(self, paper_client):
        # Paper mode → straight delegate to place_order (PAPER result).
        res = await paper_client.place_order_with_timeout(_gtc_order())
        assert res["status"] == "PAPER"

    @pytest.mark.asyncio
    async def test_fok_delegates_no_cancel(self):
        c = _orchestrator_client()
        c.place_order = AsyncMock(
            return_value={"status": "LIVE", "order_id": "o1", "filled_size": 200.0, "avg_price": 0.50}
        )
        c.cancel_order = AsyncMock()
        c.get_order = AsyncMock()
        order = Order(market_id="m", token_id="t", side="BUY", price=0.50, size_usdc=100.0, order_type="FOK")
        await c.place_order_with_timeout(order)
        c.place_order.assert_awaited_once()  # single shot
        c.cancel_order.assert_not_awaited()
        c.get_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_timeout_zero_disables(self):
        c = _orchestrator_client(timeout=0.0)
        c.place_order = AsyncMock(
            return_value={"status": "LIVE", "order_id": "o1", "filled_size": 0.0, "avg_price": None}
        )
        c.cancel_order = AsyncMock()
        await c.place_order_with_timeout(_gtc_order())
        c.place_order.assert_awaited_once()
        c.cancel_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_filled_within_timeout_no_retry(self):
        c = _orchestrator_client()
        # First post rests (0), then get_order reports a full fill → no cancel/retry.
        c.place_order = AsyncMock(
            return_value={"status": "LIVE", "order_id": "o1", "filled_size": 0.0, "avg_price": None}
        )
        c.get_order = AsyncMock(return_value={"filled_size": 200.0, "avg_price": 0.50})
        c.cancel_order = AsyncMock()
        res = await c.place_order_with_timeout(_gtc_order())
        assert res["filled_size"] == 200.0
        c.cancel_order.assert_not_awaited()
        assert c.place_order.await_count == 1  # no retry

    @pytest.mark.asyncio
    async def test_unfilled_cancels_and_retries_remainder(self):
        c = _orchestrator_client()
        c.place_order = AsyncMock(
            side_effect=[
                {"status": "LIVE", "order_id": "o1", "filled_size": 0.0, "avg_price": None},
                {"status": "LIVE", "order_id": "o2", "filled_size": 200.0, "avg_price": 0.52},
            ]
        )
        c.get_order = AsyncMock(return_value={"filled_size": 0.0, "avg_price": None})
        c.cancel_order = AsyncMock(return_value=True)
        res = await c.place_order_with_timeout(_gtc_order(price=0.50, size_usdc=100.0))
        # Retry placed; second place_order sized to the FULL remainder (200 shares
        # → 100 usdc) at the WIDER retry slippage.
        assert c.place_order.await_count == 2
        retry_call = c.place_order.await_args_list[1]
        retry_order = retry_call.args[0]
        assert retry_order.size_usdc == pytest.approx(100.0)  # 200 shares * 0.50
        assert retry_call.kwargs["slippage_override"] == 0.02  # wider cap
        assert res["filled_size"] == pytest.approx(200.0)
        assert res["avg_price"] == pytest.approx(0.52)

    @pytest.mark.asyncio
    async def test_retry_sizes_only_remaining_shares(self):
        # Attempt 1 partially fills 40/200; the retry must request only 160 shares.
        c = _orchestrator_client()
        c.place_order = AsyncMock(
            side_effect=[
                {"status": "LIVE", "order_id": "o1", "filled_size": 40.0, "avg_price": 0.50},
                {"status": "LIVE", "order_id": "o2", "filled_size": 160.0, "avg_price": 0.52},
            ]
        )
        c.get_order = AsyncMock(return_value={"filled_size": 40.0, "avg_price": 0.50})
        c.cancel_order = AsyncMock(return_value=True)
        res = await c.place_order_with_timeout(_gtc_order(price=0.50, size_usdc=100.0))
        retry_order = c.place_order.await_args_list[1].args[0]
        assert retry_order.size_usdc == pytest.approx(80.0)  # 160 shares * 0.50, NOT 200
        # Merged: 40@0.50 + 160@0.52 = 200 shares, size-weighted avg 0.516.
        assert res["filled_size"] == pytest.approx(200.0)
        assert res["avg_price"] == pytest.approx((40 * 0.50 + 160 * 0.52) / 200)

    @pytest.mark.asyncio
    async def test_no_retry_when_cancel_fails(self):
        c = _orchestrator_client()
        c.place_order = AsyncMock(
            return_value={"status": "LIVE", "order_id": "o1", "filled_size": 0.0, "avg_price": None}
        )
        c.get_order = AsyncMock(return_value={"filled_size": 0.0, "avg_price": None})
        c.cancel_order = AsyncMock(return_value=False)  # cancel fails → ambiguous
        res = await c.place_order_with_timeout(_gtc_order())
        assert c.place_order.await_count == 1  # NO second order
        assert res["filled_size"] == 0.0

    @pytest.mark.asyncio
    async def test_no_retry_when_confirm_ambiguous(self):
        c = _orchestrator_client()
        c.place_order = AsyncMock(
            return_value={"status": "LIVE", "order_id": "o1", "filled_size": 0.0, "avg_price": None}
        )
        # get_order can never give a concrete fill (None) → ambiguous → never retry.
        c.get_order = AsyncMock(return_value=None)
        c.cancel_order = AsyncMock(return_value=True)
        res = await c.place_order_with_timeout(_gtc_order())
        assert c.place_order.await_count == 1  # NO retry on ambiguous confirm
        assert res["filled_size"] == 0.0

    @pytest.mark.asyncio
    async def test_no_retry_when_remainder_below_min(self):
        # Confirmed fill leaves < _MIN_RETRY_SHARES unfilled → no second round-trip.
        c = _orchestrator_client()
        c.place_order = AsyncMock(
            return_value={"status": "LIVE", "order_id": "o1", "filled_size": 199.5, "avg_price": 0.50}
        )
        c.get_order = AsyncMock(return_value={"filled_size": 199.5, "avg_price": 0.50})
        c.cancel_order = AsyncMock(return_value=True)
        res = await c.place_order_with_timeout(_gtc_order(price=0.50, size_usdc=100.0))
        assert c.place_order.await_count == 1
        assert res["filled_size"] == pytest.approx(199.5)

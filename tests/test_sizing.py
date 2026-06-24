"""Tests for the pure Kelly position-sizing module."""

from __future__ import annotations

import pytest

from polymarket_copier.core.sizing import (
    edge_to_win_prob,
    kelly_fraction,
    kelly_size_from_edge,
    kelly_size_usdc,
    roi_to_edge,
)


class TestKellyFraction:
    def test_known_value_even_money(self):
        # price=0.5 → b=1. f* = p - (1-p) = 2p - 1. For p=0.6 → 0.2.
        assert kelly_fraction(0.6, 0.5) == pytest.approx(0.2)

    def test_known_value_favourite(self):
        # price=0.4 → b=(0.6/0.4)=1.5. f* = p - (1-p)/b.
        # p=0.7 → 0.7 - 0.3/1.5 = 0.7 - 0.2 = 0.5
        assert kelly_fraction(0.7, 0.4) == pytest.approx(0.5)

    def test_no_edge_returns_zero(self):
        # Fair bet: p == price → f* = 0.
        assert kelly_fraction(0.5, 0.5) == pytest.approx(0.0)

    def test_negative_edge_clamped_to_zero(self):
        # p below break-even → negative f* clamped to 0.
        assert kelly_fraction(0.3, 0.5) == 0.0

    def test_degenerate_price_zero(self):
        assert kelly_fraction(0.7, 0.0) == 0.0

    def test_degenerate_price_one(self):
        assert kelly_fraction(0.7, 1.0) == 0.0

    def test_degenerate_price_out_of_range(self):
        assert kelly_fraction(0.7, 1.5) == 0.0
        assert kelly_fraction(0.7, -0.1) == 0.0

    def test_degenerate_win_prob_out_of_range(self):
        assert kelly_fraction(1.5, 0.5) == 0.0
        assert kelly_fraction(-0.1, 0.5) == 0.0


class TestKellySizeUsdc:
    def test_fractional_multiplier_scales_down(self):
        # f*=0.2 at p=0.6, price=0.5. bankroll=10k, mult=0.25, generous cap.
        # raw = 10000 * 0.2 * 0.25 = 500. Cap = 10000 * 0.10 = 1000 → 500.
        size = kelly_size_usdc(0.6, 0.5, 10_000, kelly_multiplier=0.25, max_pct=0.10)
        assert size == pytest.approx(500.0)

    def test_clamped_to_max_pct(self):
        # Big edge: f*=0.5 at p=0.7, price=0.4. raw=10000*0.5*0.25=1250.
        # Cap = 2% of 10k = 200 → clamped.
        size = kelly_size_usdc(0.7, 0.4, 10_000, kelly_multiplier=0.25, max_pct=0.02)
        assert size == pytest.approx(200.0)

    def test_no_edge_returns_zero(self):
        assert kelly_size_usdc(0.5, 0.5, 10_000) == 0.0

    def test_negative_edge_returns_zero(self):
        assert kelly_size_usdc(0.3, 0.5, 10_000) == 0.0

    def test_degenerate_bankroll(self):
        assert kelly_size_usdc(0.7, 0.4, 0.0) == 0.0
        assert kelly_size_usdc(0.7, 0.4, -100) == 0.0

    def test_degenerate_multiplier(self):
        assert kelly_size_usdc(0.7, 0.4, 10_000, kelly_multiplier=0.0) == 0.0

    def test_degenerate_price(self):
        assert kelly_size_usdc(0.7, 0.0, 10_000) == 0.0
        assert kelly_size_usdc(0.7, 1.0, 10_000) == 0.0


class TestRoiToEdge:
    """H18: edge = max(0, mean_roi) * price (from E[ROI] = edge/price)."""

    def test_positive_roi(self):
        assert roi_to_edge(0.40, 0.50) == pytest.approx(0.20)
        assert roi_to_edge(0.10, 0.80) == pytest.approx(0.08)

    def test_negative_roi_is_zero_edge(self):
        assert roi_to_edge(-0.30, 0.50) == 0.0

    def test_zero_roi_is_zero_edge(self):
        assert roi_to_edge(0.0, 0.50) == 0.0

    def test_degenerate_price(self):
        assert roi_to_edge(0.40, 0.0) == 0.0
        assert roi_to_edge(0.40, 1.0) == 0.0

    def test_non_finite_roi(self):
        assert roi_to_edge(float("nan"), 0.50) == 0.0
        assert roi_to_edge(float("inf"), 0.50) == 0.0


class TestEdgeToWinProb:
    """H18: p = clamp(price + min(max(0,edge)*shrink, max_edge), 0, 1)."""

    def test_basic(self):
        # edge 0.20, shrink 0.5 → 0.10 (≤ max_edge); p = 0.50 + 0.10 = 0.60
        assert edge_to_win_prob(0.20, 0.50, edge_shrink=0.5, max_edge=0.20) == pytest.approx(0.60)

    def test_shrink_default_is_one(self):
        assert edge_to_win_prob(0.10, 0.50) == pytest.approx(0.60)

    def test_max_edge_caps(self):
        # huge edge clamps to max_edge=0.20 above price
        assert edge_to_win_prob(5.0, 0.50, edge_shrink=1.0, max_edge=0.20) == pytest.approx(0.70)

    def test_negative_edge_is_price(self):
        # no edge → p == price (Kelly fraction will be 0)
        assert edge_to_win_prob(-0.10, 0.50) == pytest.approx(0.50)

    def test_clamped_to_unit_interval(self):
        assert edge_to_win_prob(5.0, 0.97, edge_shrink=1.0, max_edge=0.50) <= 1.0

    def test_degenerate_price_and_non_finite(self):
        assert edge_to_win_prob(0.20, 0.0) == 0.0
        assert edge_to_win_prob(float("nan"), 0.50) == 0.0


class TestKellySizeFromEdge:
    def test_reuses_kelly_math(self):
        # edge 0.20 @ price 0.50, shrink 0.5 → p=0.60 → f*=0.20 → raw=$500, cap $200.
        size = kelly_size_from_edge(0.20, 0.50, 10_000, 0.25, 0.02, edge_shrink=0.5, max_edge=0.20)
        assert size == pytest.approx(200.0)

    def test_zero_edge_returns_zero(self):
        assert kelly_size_from_edge(0.0, 0.50, 10_000) == 0.0

    def test_negative_edge_returns_zero(self):
        assert kelly_size_from_edge(-0.50, 0.50, 10_000) == 0.0

    def test_degenerate_inputs(self):
        assert kelly_size_from_edge(0.20, 0.50, 0.0) == 0.0
        assert kelly_size_from_edge(0.20, 0.0, 10_000) == 0.0
        assert kelly_size_from_edge(0.20, 0.50, 10_000, kelly_multiplier=0.0) == 0.0

    def test_small_edge_sub_cap_size(self):
        # A small edge produces a precise, sub-cap size (exercises the non-capped path).
        # edge 0.02 @ 0.50, shrink 1.0 → p=0.52 → f* = 0.52 - 0.48*0.50/0.50 = 0.04
        # raw = 10000 * 0.04 * 0.25 = $100 (< $200 cap).
        size = kelly_size_from_edge(0.02, 0.50, 10_000, 0.25, 0.02, edge_shrink=1.0, max_edge=0.20)
        assert size == pytest.approx(100.0)

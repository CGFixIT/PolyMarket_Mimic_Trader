"""Tests for the pure Kelly position-sizing module."""

from __future__ import annotations

import pytest

from polymarket_copier.core.sizing import kelly_fraction, kelly_size_usdc


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

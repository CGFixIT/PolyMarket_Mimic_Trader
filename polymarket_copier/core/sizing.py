"""Edge-aware position sizing via the Kelly criterion.

Polymarket binary tokens pay $1 on resolution-in-the-money and $0 otherwise.
Buying one share at ``price`` (with ``0 < price < 1``) is therefore a bet with:

    cost           = price
    payout on win  = 1 - price   (net profit)
    payout on loss = -price      (the stake)
    net odds       b = (1 - price) / price

For a win probability ``p`` the Kelly criterion maximises the expected log
growth of bankroll by wagering the fraction:

    f* = p - (1 - p) / b
       = p - (1 - p) * price / (1 - price)

``f*`` is the *full* Kelly fraction. It is the growth-optimal bet only when the
edge (``p``) is known exactly. In practice ``p`` is an *estimate* (here, an
observed win rate over a finite sample), and Kelly is famously sensitive to
overestimation of the edge: betting full Kelly on an inflated ``p`` produces
severe drawdowns. The standard mitigation is *fractional* Kelly — scaling
``f*`` by a conservative multiplier (e.g. 0.25) — which sharply reduces variance
at a modest cost to growth. Callers should also gate on a minimum sample size
before trusting an observed win rate.

A non-positive ``f*`` means there is no edge at this price; we never bet in that
case (return 0).

EDGE-BASED SIZING (H18)
-----------------------
Passing a trader's observed WIN RATE in as ``p`` is wrong: for a binary token
paying $1 the market-implied probability is the price itself, so a favorite-buyer
(high win rate, ~zero edge) gets oversized. The correct ``p`` is
``clamp(price + edge, 0, 1)`` where ``edge`` is the trader's DEMONSTRATED
probability edge. From E[ROI] = edge / price (buying 1 share at ``price`` that
pays $1 with probability ``p = price + edge``), the natural conversion is
``edge = mean_roi * price``. ``edge_to_win_prob`` / ``kelly_size_from_edge``
implement this and reuse ``kelly_fraction`` / ``kelly_size_usdc`` unchanged.
"""

from __future__ import annotations

import math


def kelly_fraction(win_prob: float, price: float) -> float:
    """Return the full Kelly fraction for a binary token, clamped at 0.

    Args:
        win_prob: Estimated probability of winning, in [0, 1].
        price: Token entry price, in (0, 1).

    Returns:
        ``max(0.0, f*)`` where ``f* = p - (1 - p) * price / (1 - price)``.
        Returns 0.0 for degenerate inputs (price outside (0, 1) or
        win_prob outside [0, 1]).
    """
    if not (0.0 < price < 1.0):
        return 0.0
    if not (0.0 <= win_prob <= 1.0):
        return 0.0

    # b = (1 - price) / price; f* = p - (1 - p) / b
    f_star = win_prob - (1.0 - win_prob) * price / (1.0 - price)
    return max(0.0, f_star)


def kelly_size_usdc(
    win_prob: float,
    price: float,
    bankroll: float,
    kelly_multiplier: float = 0.25,
    max_pct: float = 0.02,
) -> float:
    """Size a copy trade in USDC using fractional Kelly, clamped to a hard cap.

    Args:
        win_prob: Estimated win probability, in [0, 1].
        price: Token entry price, in (0, 1).
        bankroll: Current bankroll in USDC.
        kelly_multiplier: Fractional-Kelly scaler (default 0.25, conservative).
        max_pct: Hard ceiling as a fraction of bankroll (e.g. 0.02 = 2%).

    Returns:
        A USDC notional in ``[0, bankroll * max_pct]``. Returns 0.0 on any
        degenerate input (non-positive bankroll/multiplier, or no edge).
    """
    if bankroll <= 0.0 or kelly_multiplier <= 0.0 or max_pct <= 0.0:
        return 0.0

    f_star = kelly_fraction(win_prob, price)
    if f_star <= 0.0:
        return 0.0

    raw = bankroll * f_star * kelly_multiplier
    cap = bankroll * max_pct
    return max(0.0, min(raw, cap))


def roi_to_edge(mean_roi: float, price: float) -> float:
    """Convert a trader's mean per-trade ROI fraction to a raw probability edge (H18).

    Derived from E[ROI] = edge / price for a binary $1 token bought at ``price``:
    ``edge = max(0, mean_roi) * price``. Negative/zero ROI (no demonstrated skill)
    yields 0 edge. Returns 0.0 for a degenerate price or non-finite ROI.
    """
    if not (0.0 < price < 1.0) or not math.isfinite(mean_roi):
        return 0.0
    return max(0.0, mean_roi) * price


def edge_to_win_prob(
    edge: float,
    price: float,
    *,
    edge_shrink: float = 1.0,
    max_edge: float = 0.20,
) -> float:
    """Convert a probability edge into a Kelly win-prob ``p = clamp(price + edge, 0, 1)`` (H18).

    ``edge`` is in probability points (same units as price). It is floored at 0,
    scaled by the conservative ``edge_shrink`` (Kelly punishes overestimated edge),
    and capped at ``max_edge`` so one lucky high-ROI sample can't push ``p`` to an
    extreme. Returns 0.0 for a degenerate price or non-finite edge.
    """
    if not (0.0 < price < 1.0) or not math.isfinite(edge):
        return 0.0
    e = max(0.0, edge) * max(0.0, edge_shrink)
    e = min(e, max_edge)
    p = price + e
    return min(1.0, max(0.0, p))


def kelly_size_from_edge(
    edge: float,
    price: float,
    bankroll: float,
    kelly_multiplier: float = 0.25,
    max_pct: float = 0.02,
    *,
    edge_shrink: float = 1.0,
    max_edge: float = 0.20,
) -> float:
    """Size a copy trade from a probability edge using fractional Kelly (H18).

    Computes ``p = clamp(price + shrunk_capped_edge, 0, 1)`` then delegates to
    ``kelly_size_usdc`` (reusing the existing Kelly math). A non-positive edge gives
    ``p <= price`` → no edge → 0 size. Returns 0.0 on any degenerate input.
    """
    if bankroll <= 0.0 or kelly_multiplier <= 0.0 or max_pct <= 0.0:
        return 0.0
    p = edge_to_win_prob(edge, price, edge_shrink=edge_shrink, max_edge=max_edge)
    if p <= 0.0:
        return 0.0
    return kelly_size_usdc(p, price, bankroll, kelly_multiplier, max_pct)

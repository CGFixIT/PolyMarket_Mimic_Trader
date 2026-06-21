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
"""

from __future__ import annotations


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

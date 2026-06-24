"""Tests for v2 risk-adjusted trader scoring."""

from __future__ import annotations

import time

import pytest

from polymarket_copier.core.tracker import (
    ScoredTrader,
    TraderScorer,
    TraderStats,
    TrackerConfig,
    _compute_trader_stats,
    _parse_timestamp,
)


def make_stats(
    pnl=50000, win_rate=0.65, trades=200, pnl_list=None, last_trade=None,
) -> TraderStats:
    if pnl_list is None:
        pnl_list = [10.0, -5.0, 20.0, 15.0, -8.0]
    if last_trade is None:
        last_trade = time.time() - 3600  # 1 hour ago
    return TraderStats(
        address="0xabc",
        pseudonym="Tester",
        total_pnl=pnl,
        trade_count=trades,
        win_rate=win_rate,
        pnl_per_trade=pnl_list,
        last_trade_time=last_trade,
    )


class TestTraderStats:
    def test_mean_pnl(self):
        stats = make_stats(pnl_list=[10.0, 20.0, 30.0])
        assert stats.mean_pnl == 20.0

    def test_stddev_pnl(self):
        stats = make_stats(pnl_list=[10.0, 20.0, 30.0])
        assert stats.stddev_pnl > 0

    def test_stddev_single_value_zero(self):
        stats = make_stats(pnl_list=[10.0])
        assert stats.stddev_pnl == 0.0

    def test_sharpe_proxy_positive(self):
        stats = make_stats(pnl_list=[10.0, 12.0, 11.0, 13.0])
        assert stats.sharpe_proxy > 0

    def test_sharpe_proxy_zero_variance_positive_mean(self):
        stats = make_stats(pnl_list=[10.0, 10.0])
        # Zero variance with positive mean → large positive
        assert stats.sharpe_proxy > 0


class TestTraderScorer:
    def test_eligible_trader_scored(self):
        scorer = TraderScorer(TrackerConfig())
        result = scorer.score(make_stats())
        assert result is not None
        assert isinstance(result, ScoredTrader)
        assert result.score > 0

    def test_ineligible_low_pnl(self):
        scorer = TraderScorer(TrackerConfig())
        result = scorer.score(make_stats(pnl=500))
        assert result is None

    def test_ineligible_low_win_rate(self):
        # H16: min_win_rate is no longer a hard gate; replaced with min_expectancy.
        # A 40% win rate trader with positive mean_pnl should pass (expectancy > 0.01).
        # Test low expectancy instead: mean_pnl so small that mean_pnl * log(n+1) < 0.01.
        scorer = TraderScorer(TrackerConfig())
        # mean_pnl = 0.0001; expectancy = 0.0001 * log(201) ~ 0.00053 < 0.01
        result = scorer.score(make_stats(pnl_list=[0.0001, 0.0001, 0.0001, 0.0001, 0.0001]))
        assert result is None

    def test_ineligible_few_trades(self):
        scorer = TraderScorer(TrackerConfig())
        result = scorer.score(make_stats(trades=10))
        assert result is None

    def test_recency_weight_decays(self):
        scorer = TraderScorer(TrackerConfig(half_life_days=14))
        recent = scorer._recency_weight(time.time() - 3600)       # ~1.0
        old = scorer._recency_weight(time.time() - 14 * 86400)    # ~0.5
        assert recent > old
        assert abs(old - 0.5) < 0.05

    def test_recency_weight_never_traded(self):
        scorer = TraderScorer(TrackerConfig())
        assert scorer._recency_weight(0) == 0.0

    def test_score_many_ranks_and_caps(self):
        scorer = TraderScorer(TrackerConfig(max_top_traders=2))
        stats = [
            make_stats(pnl_list=[20.0, 21.0, 19.0, 20.5]),  # consistent → high sharpe
            make_stats(pnl_list=[100.0, -90.0, 80.0, -70.0]),  # volatile → low sharpe
            make_stats(pnl_list=[15.0, 16.0, 14.0, 15.5]),
        ]
        ranked = scorer.score_many(stats)
        assert len(ranked) == 2  # capped at max_top_traders
        assert ranked[0].rank == 1
        assert ranked[1].rank == 2
        assert ranked[0].score >= ranked[1].score


class TestComputeTraderStats:
    def test_round_trip_win(self, sample_activity):
        stats = _compute_trader_stats("0xabc", "Name", 50000, sample_activity)
        assert stats.trade_count == 1
        assert stats.win_rate == 1.0

    def test_no_trades_falls_back(self):
        stats = _compute_trader_stats("0xabc", "Name", 50000, [])
        assert stats.win_rate == 0.0
        assert stats.pnl_per_trade == []

    def test_malformed_price_is_skipped_not_fatal(self):
        # A record with a non-numeric price must be skipped silently; the valid
        # round-trip should still be counted. Robustness against dirty API data.
        activity = [
            {"id": "bad", "type": "trade", "side": "BUY", "market": "m",
             "asset": "a", "price": "not-a-number", "size": "10",
             "timestamp": 1_700_000_000},
            {"id": "b1", "type": "trade", "side": "BUY", "market": "m",
             "asset": "a", "price": "0.50", "size": "100",
             "timestamp": 1_700_000_000},
            {"id": "s1", "type": "trade", "side": "SELL", "market": "m",
             "asset": "a", "price": "0.60", "size": "60",
             "timestamp": 1_700_001_000},
        ]
        stats = _compute_trader_stats("0xabc", "Name", 50000, activity)
        assert stats.trade_count == 1
        assert stats.win_rate == 1.0

    def test_non_trade_types_ignored(self):
        activity = [
            {"id": "x", "type": "transfer", "side": "BUY", "market": "m",
             "asset": "a", "price": "0.5", "size": "10"},
        ]
        stats = _compute_trader_stats("0xabc", "Name", 50000, activity)
        assert stats.pnl_per_trade == []

    def test_buy_then_redeem_at_one_is_winning_trade(self):
        # Held-to-resolution: buy a token at 0.50, redeem at $1.00 payout.
        # 100 / 0.50 = 200 shares; pnl = (1.0 - 0.5) * 200 = 100.0.
        activity = [
            {"id": "b1", "type": "trade", "side": "BUY", "market": "m",
             "asset": "a", "price": "0.50", "size": "100",
             "timestamp": 1_700_000_000},
            {"id": "r1", "type": "redeem", "market": "m", "asset": "a",
             "price": "1.0", "size": "200", "timestamp": 1_700_002_000},
        ]
        stats = _compute_trader_stats("0xabc", "Name", 50000, activity)
        assert stats.trade_count == 1
        assert stats.win_rate == 1.0
        # buy_shares = 100 / 0.50 = 200; pnl_dollars = (1.0 - 0.5) * 200 = 100.0
        # cost_basis = 0.50 * 200 = 100.0; roi = 100.0 / 100.0 = 1.0 (100% return)
        assert stats.pnl_per_trade == [pytest.approx(1.0)]

    def test_redeem_defaults_to_payout_one_when_no_price(self):
        # No explicit per-share price on the redeem record → default to 1.0.
        activity = [
            {"id": "b1", "type": "trade", "side": "BUY", "market": "m",
             "asset": "a", "price": "0.40", "size": "40",
             "timestamp": 1_700_000_000},
            {"id": "r1", "type": "claim", "market": "m", "asset": "a",
             "timestamp": 1_700_002_000},
        ]
        stats = _compute_trader_stats("0xabc", "Name", 50000, activity)
        assert stats.trade_count == 1
        assert stats.win_rate == 1.0
        # buy_shares = 40 / 0.40 = 100; pnl_dollars = (1.0 - 0.40) * 100 = 60.0
        # cost_basis = 0.40 * 100 = 40.0; roi = 60.0 / 40.0 = 1.5 (150% return)
        assert stats.pnl_per_trade == [pytest.approx(1.5)]

    def test_buy_without_sell_or_redeem_not_counted(self):
        # Unchanged behavior: an open buy with no realizing event is excluded.
        activity = [
            {"id": "b1", "type": "trade", "side": "BUY", "market": "m",
             "asset": "a", "price": "0.50", "size": "100",
             "timestamp": 1_700_000_000},
        ]
        stats = _compute_trader_stats("0xabc", "Name", 50000, activity)
        # No realizing event → no TradeRecord produced (no win/loss credited).
        # (trade_count falls back to len(activity) when there are zero records,
        # matching the pre-existing no-trades fallback path — unchanged.)
        assert stats.pnl_per_trade == []
        assert stats.win_rate == 0.0

    def test_mixed_sell_round_trip_and_held_to_resolution_redeem(self):
        # One actively-sold round-trip plus one held-to-resolution redeem on a
        # different market — both must be counted.
        activity = [
            {"id": "b1", "type": "trade", "side": "BUY", "market": "m1",
             "asset": "a1", "price": "0.50", "size": "100",
             "timestamp": 1_700_000_000},
            {"id": "s1", "type": "trade", "side": "SELL", "market": "m1",
             "asset": "a1", "price": "0.60", "size": "60",
             "timestamp": 1_700_001_000},
            {"id": "b2", "type": "trade", "side": "BUY", "market": "m2",
             "asset": "a2", "price": "0.20", "size": "20",
             "timestamp": 1_700_000_500},
            {"id": "r2", "type": "redeem", "market": "m2", "asset": "a2",
             "price": "1.0", "size": "100", "timestamp": 1_700_003_000},
        ]
        stats = _compute_trader_stats("0xabc", "Name", 50000, activity)
        assert stats.trade_count == 2
        assert stats.win_rate == 1.0
        # round-trip: pnl_dollars=20.0, cost_basis=100.0, roi=0.2
        # redeem: pnl_dollars=80.0, cost_basis=20.0, roi=4.0
        assert sorted(stats.pnl_per_trade) == [pytest.approx(0.2), pytest.approx(4.0)]


class TestReturnsBasedScoring:
    """pnl_per_trade stores per-trade ROI (return on capital), not dollars."""

    @staticmethod
    def _round_trip(market, entry_price, notional_usdc, exit_price):
        """One BUY/SELL pair. notional_usdc = entry_price * shares (cost basis)."""
        return [
            {"id": f"b-{market}", "type": "trade", "side": "BUY",
             "market": market, "asset": f"tok-{market}",
             "price": str(entry_price), "size": str(notional_usdc),
             "timestamp": 1_700_000_000},
            {"id": f"s-{market}", "type": "trade", "side": "SELL",
             "market": market, "asset": f"tok-{market}",
             "price": str(exit_price), "size": "1", "timestamp": 1_700_001_000},
        ]

    def test_roi_stored_not_dollars(self):
        # Buy 100 USDC @ 0.50 -> 200 shares; sell @ 0.65.
        # dollars = (0.65-0.50)*200 = 30; cost_basis = 0.50*200 = 100; roi = 0.30
        stats = _compute_trader_stats(
            "0xabc", "Name", 50000, self._round_trip("m", 0.50, 100, 0.65)
        )
        assert stats.pnl_per_trade == [pytest.approx(0.30)]

    def test_same_roi_different_notionals_is_size_independent(self):
        # Three +10% trades on wildly different notionals must yield identical
        # per-trade returns, proving the score no longer tracks position size.
        activity = (
            self._round_trip("small", 0.20, 10, 0.22)      # +10%
            + self._round_trip("medium", 0.50, 1_000, 0.55)  # +10%
            + self._round_trip("large", 0.80, 100_000, 0.88)  # +10%
        )
        stats = _compute_trader_stats("0xabc", "Name", 50000, activity)
        assert stats.trade_count == 3
        assert stats.pnl_per_trade == [
            pytest.approx(0.10), pytest.approx(0.10), pytest.approx(0.10)
        ]
        # Identical returns -> zero variance, positive mean.
        assert stats.mean_pnl == pytest.approx(0.10)
        assert stats.stddev_pnl == pytest.approx(0.0)
        assert stats.win_rate == 1.0

    def test_mean_stddev_sharpe_reflect_returns(self):
        # +20% then -10% round-trips.
        activity = (
            self._round_trip("a", 0.50, 1_000, 0.60)   # +20%
            + self._round_trip("b", 0.50, 1_000, 0.45)  # -10%
        )
        stats = _compute_trader_stats("0xabc", "Name", 50000, activity)
        assert stats.pnl_per_trade == [pytest.approx(0.20), pytest.approx(-0.10)]
        assert stats.mean_pnl == pytest.approx(0.05)
        # sharpe = mean / stddev, both on returns
        assert stats.sharpe_proxy == pytest.approx(
            stats.mean_pnl / stats.stddev_pnl
        )

    def test_loss_has_negative_roi_and_not_a_win(self):
        stats = _compute_trader_stats(
            "0xabc", "Name", 50000, self._round_trip("m", 0.50, 100, 0.40)
        )
        assert stats.pnl_per_trade == [pytest.approx(-0.20)]
        assert stats.win_rate == 0.0

    def test_total_pnl_stays_dollar_aggregate(self):
        # total_pnl is the leaderboard dollar figure, untouched by ROI scoring.
        stats = _compute_trader_stats(
            "0xabc", "Name", 50000, self._round_trip("m", 0.50, 100, 0.65)
        )
        assert stats.total_pnl == 50000
        # ...while per-trade values are fractional returns, not dollars.
        assert max(stats.pnl_per_trade) < 1.0


class TestParseTimestamp:
    def test_iso_string(self):
        assert _parse_timestamp("2023-11-14T22:13:20+00:00") == pytest.approx(
            1_700_000_000, abs=1
        )

    def test_invalid_string_returns_current_time(self):
        before = time.time()
        result = _parse_timestamp("garbage")
        after = time.time()
        assert before <= result <= after

    def test_millis_normalized_to_seconds(self):
        assert _parse_timestamp(1_700_000_000_000) == pytest.approx(
            1_700_000_000, abs=1
        )

    def test_seconds_passthrough(self):
        assert _parse_timestamp(1_700_000_000) == pytest.approx(1_700_000_000)

    def test_unsupported_type_returns_current_time(self):
        before = time.time()
        result = _parse_timestamp(None)
        after = time.time()
        assert before <= result <= after

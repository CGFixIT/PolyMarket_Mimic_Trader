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
        scorer = TraderScorer(TrackerConfig())
        result = scorer.score(make_stats(win_rate=0.40))
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
        assert stats.pnl_per_trade == [100.0]

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
        # 40 / 0.40 = 100 shares; pnl = (1.0 - 0.40) * 100 = 60.0
        assert stats.pnl_per_trade == [pytest.approx(60.0)]

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
        # round-trip: (0.60-0.50)*(100/0.50)=20.0 ; redeem: (1.0-0.20)*(20/0.20)=80.0
        assert sorted(stats.pnl_per_trade) == [pytest.approx(20.0), pytest.approx(80.0)]


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

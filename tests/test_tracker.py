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

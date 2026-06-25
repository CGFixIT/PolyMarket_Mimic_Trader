"""Tests for v2 risk-adjusted trader scoring."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from polymarket_copier.core.tracker import (
    ScoredTrader,
    TraderScorer,
    TraderStats,
    TrackerClient,
    TrackerConfig,
    _compute_trader_stats,
    _parse_timestamp,
)


def make_stats(
    pnl=50000,
    win_rate=0.65,
    trades=200,
    pnl_list=None,
    last_trade=None,
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
        recent = scorer._recency_weight(time.time() - 3600)  # ~1.0
        old = scorer._recency_weight(time.time() - 14 * 86400)  # ~0.5
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

    def test_typical_trade_size_is_median_of_buys(self):
        # M4: typical_trade_size is the median USDC notional of the trader's BUYs.
        activity = [
            {
                "id": f"b{i}",
                "type": "trade",
                "side": "BUY",
                "market": "m",
                "asset": f"a{i}",
                "price": "0.50",
                "size": str(sz),
                "timestamp": 1_700_000_000,
            }
            for i, sz in enumerate([100, 200, 900])
        ]
        stats = _compute_trader_stats("0xabc", "Name", 50000, activity)
        assert stats.typical_trade_size == 200.0  # median of {100, 200, 900}, outlier-robust

    def test_typical_trade_size_zero_without_buys(self):
        # No BUY records → typical size is 0.0 (conviction signal then no-ops).
        stats = _compute_trader_stats("0xabc", "Name", 50000, [])
        assert stats.typical_trade_size == 0.0

    def test_malformed_price_is_skipped_not_fatal(self):
        # A record with a non-numeric price must be skipped silently; the valid
        # round-trip should still be counted. Robustness against dirty API data.
        activity = [
            {
                "id": "bad",
                "type": "trade",
                "side": "BUY",
                "market": "m",
                "asset": "a",
                "price": "not-a-number",
                "size": "10",
                "timestamp": 1_700_000_000,
            },
            {
                "id": "b1",
                "type": "trade",
                "side": "BUY",
                "market": "m",
                "asset": "a",
                "price": "0.50",
                "size": "100",
                "timestamp": 1_700_000_000,
            },
            {
                "id": "s1",
                "type": "trade",
                "side": "SELL",
                "market": "m",
                "asset": "a",
                "price": "0.60",
                "size": "60",
                "timestamp": 1_700_001_000,
            },
        ]
        stats = _compute_trader_stats("0xabc", "Name", 50000, activity)
        assert stats.trade_count == 1
        assert stats.win_rate == 1.0

    def test_non_trade_types_ignored(self):
        activity = [
            {"id": "x", "type": "transfer", "side": "BUY", "market": "m", "asset": "a", "price": "0.5", "size": "10"},
        ]
        stats = _compute_trader_stats("0xabc", "Name", 50000, activity)
        assert stats.pnl_per_trade == []

    def test_buy_then_redeem_at_one_is_winning_trade(self):
        # Held-to-resolution: buy a token at 0.50, redeem at $1.00 payout.
        # 100 / 0.50 = 200 shares; pnl = (1.0 - 0.5) * 200 = 100.0.
        activity = [
            {
                "id": "b1",
                "type": "trade",
                "side": "BUY",
                "market": "m",
                "asset": "a",
                "price": "0.50",
                "size": "100",
                "timestamp": 1_700_000_000,
            },
            {
                "id": "r1",
                "type": "redeem",
                "market": "m",
                "asset": "a",
                "price": "1.0",
                "size": "200",
                "timestamp": 1_700_002_000,
            },
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
            {
                "id": "b1",
                "type": "trade",
                "side": "BUY",
                "market": "m",
                "asset": "a",
                "price": "0.40",
                "size": "40",
                "timestamp": 1_700_000_000,
            },
            {"id": "r1", "type": "claim", "market": "m", "asset": "a", "timestamp": 1_700_002_000},
        ]
        stats = _compute_trader_stats("0xabc", "Name", 50000, activity)
        assert stats.trade_count == 1
        assert stats.win_rate == 1.0
        # buy_shares = 40 / 0.40 = 100; pnl_dollars = (1.0 - 0.40) * 100 = 60.0
        # cost_basis = 0.40 * 100 = 40.0; roi = 60.0 / 40.0 = 1.5 (150% return)
        assert stats.pnl_per_trade == [pytest.approx(1.5)]

    def test_buy_without_sell_or_redeem_not_counted_when_imputation_disabled(self):
        # impute_loss_after_days=0.0 disables imputation: open buys remain uncounted.
        activity = [
            {
                "id": "b1",
                "type": "trade",
                "side": "BUY",
                "market": "m",
                "asset": "a",
                "price": "0.50",
                "size": "100",
                "timestamp": 1_700_000_000,
            },
        ]
        stats = _compute_trader_stats("0xabc", "Name", 50000, activity, impute_loss_after_days=0.0)
        # No realizing event → no TradeRecord produced (no win/loss credited).
        # (trade_count falls back to len(activity) when there are zero records,
        # matching the pre-existing no-trades fallback path — unchanged.)
        assert stats.pnl_per_trade == []
        assert stats.win_rate == 0.0

    def test_mixed_sell_round_trip_and_held_to_resolution_redeem(self):
        # One actively-sold round-trip plus one held-to-resolution redeem on a
        # different market — both must be counted.
        activity = [
            {
                "id": "b1",
                "type": "trade",
                "side": "BUY",
                "market": "m1",
                "asset": "a1",
                "price": "0.50",
                "size": "100",
                "timestamp": 1_700_000_000,
            },
            {
                "id": "s1",
                "type": "trade",
                "side": "SELL",
                "market": "m1",
                "asset": "a1",
                "price": "0.60",
                "size": "60",
                "timestamp": 1_700_001_000,
            },
            {
                "id": "b2",
                "type": "trade",
                "side": "BUY",
                "market": "m2",
                "asset": "a2",
                "price": "0.20",
                "size": "20",
                "timestamp": 1_700_000_500,
            },
            {
                "id": "r2",
                "type": "redeem",
                "market": "m2",
                "asset": "a2",
                "price": "1.0",
                "size": "100",
                "timestamp": 1_700_003_000,
            },
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
            {
                "id": f"b-{market}",
                "type": "trade",
                "side": "BUY",
                "market": market,
                "asset": f"tok-{market}",
                "price": str(entry_price),
                "size": str(notional_usdc),
                "timestamp": 1_700_000_000,
            },
            {
                "id": f"s-{market}",
                "type": "trade",
                "side": "SELL",
                "market": market,
                "asset": f"tok-{market}",
                "price": str(exit_price),
                "size": "1",
                "timestamp": 1_700_001_000,
            },
        ]

    def test_roi_stored_not_dollars(self):
        # Buy 100 USDC @ 0.50 -> 200 shares; sell @ 0.65.
        # dollars = (0.65-0.50)*200 = 30; cost_basis = 0.50*200 = 100; roi = 0.30
        stats = _compute_trader_stats("0xabc", "Name", 50000, self._round_trip("m", 0.50, 100, 0.65))
        assert stats.pnl_per_trade == [pytest.approx(0.30)]

    def test_same_roi_different_notionals_is_size_independent(self):
        # Three +10% trades on wildly different notionals must yield identical
        # per-trade returns, proving the score no longer tracks position size.
        activity = (
            self._round_trip("small", 0.20, 10, 0.22)  # +10%
            + self._round_trip("medium", 0.50, 1_000, 0.55)  # +10%
            + self._round_trip("large", 0.80, 100_000, 0.88)  # +10%
        )
        stats = _compute_trader_stats("0xabc", "Name", 50000, activity)
        assert stats.trade_count == 3
        assert stats.pnl_per_trade == [pytest.approx(0.10), pytest.approx(0.10), pytest.approx(0.10)]
        # Identical returns -> zero variance, positive mean.
        assert stats.mean_pnl == pytest.approx(0.10)
        assert stats.stddev_pnl == pytest.approx(0.0)
        assert stats.win_rate == 1.0

    def test_mean_stddev_sharpe_reflect_returns(self):
        # +20% then -10% round-trips.
        activity = (
            self._round_trip("a", 0.50, 1_000, 0.60)  # +20%
            + self._round_trip("b", 0.50, 1_000, 0.45)  # -10%
        )
        stats = _compute_trader_stats("0xabc", "Name", 50000, activity)
        assert stats.pnl_per_trade == [pytest.approx(0.20), pytest.approx(-0.10)]
        assert stats.mean_pnl == pytest.approx(0.05)
        # sharpe = mean / stddev, both on returns
        assert stats.sharpe_proxy == pytest.approx(stats.mean_pnl / stats.stddev_pnl)

    def test_loss_has_negative_roi_and_not_a_win(self):
        stats = _compute_trader_stats("0xabc", "Name", 50000, self._round_trip("m", 0.50, 100, 0.40))
        assert stats.pnl_per_trade == [pytest.approx(-0.20)]
        assert stats.win_rate == 0.0

    def test_total_pnl_stays_dollar_aggregate(self):
        # total_pnl is the leaderboard dollar figure, untouched by ROI scoring.
        stats = _compute_trader_stats("0xabc", "Name", 50000, self._round_trip("m", 0.50, 100, 0.65))
        assert stats.total_pnl == 50000
        # ...while per-trade values are fractional returns, not dollars.
        assert max(stats.pnl_per_trade) < 1.0


class TestParseTimestamp:
    def test_iso_string(self):
        assert _parse_timestamp("2023-11-14T22:13:20+00:00") == pytest.approx(1_700_000_000, abs=1)

    def test_invalid_string_returns_zero(self):
        # L4: invalid timestamps return 0.0 (unknown = stale) instead of time.time()
        # (which fabricated freshness and inflated recency scores).
        assert _parse_timestamp("garbage") == 0.0

    def test_millis_normalized_to_seconds(self):
        assert _parse_timestamp(1_700_000_000_000) == pytest.approx(1_700_000_000, abs=1)

    def test_seconds_passthrough(self):
        assert _parse_timestamp(1_700_000_000) == pytest.approx(1_700_000_000)

    def test_unsupported_type_returns_zero(self):
        # L4: unsupported types (None, list, etc.) return 0.0 (unknown = stale).
        assert _parse_timestamp(None) == 0.0


class TestRefreshPipeline:
    """Exercises TrackerClient.refresh() end-to-end with the network I/O stubbed,
    covering the H15 dual-window intersection and the ranking/caching steps."""

    @pytest.mark.asyncio
    async def test_refresh_returns_ranked_traders_from_dual_windows(self):
        client = TrackerClient(config=TrackerConfig(max_top_traders=5))
        all_window = [
            {"name": "0xA", "pnl": 50000, "pseudonym": "A"},
            {"name": "0xB", "pnl": 40000, "pseudonym": "B"},
        ]
        recent_window = [{"name": "0xA", "pnl": 30000}]  # only 0xA in BOTH windows
        with (
            patch.object(
                client,
                "_fetch_dual_leaderboards",
                new=AsyncMock(return_value=(all_window, recent_window)),
            ),
            patch.object(
                client,
                "_build_trader_stats",
                new=AsyncMock(side_effect=lambda session, entry: make_stats(pnl_list=[10.0, 12.0, 11.0, 13.0])),
            ),
        ):
            result = await client.refresh()
        # 0xB filtered out (not in recent window); 0xA survives and is ranked.
        assert len(result) == 1
        assert result[0].rank == 1
        assert client.top_traders == result
        assert client.last_refresh() > 0

    @pytest.mark.asyncio
    async def test_refresh_empty_when_a_window_is_empty(self):
        client = TrackerClient()
        with patch.object(
            client,
            "_fetch_dual_leaderboards",
            new=AsyncMock(return_value=([], [{"name": "0xA"}])),
        ):
            assert await client.refresh() == []

    @pytest.mark.asyncio
    async def test_refresh_empty_when_no_window_overlap(self):
        client = TrackerClient()
        with patch.object(
            client,
            "_fetch_dual_leaderboards",
            new=AsyncMock(
                return_value=(
                    [{"name": "0xA", "pnl": 50000}],
                    [{"name": "0xZ", "pnl": 50000}],
                )
            ),
        ):
            # Disjoint windows → no candidates survive the intersection.
            assert await client.refresh() == []

    @pytest.mark.asyncio
    async def test_refresh_skips_failed_stats_fetches(self):
        client = TrackerClient(config=TrackerConfig(max_top_traders=5))
        all_window = [
            {"name": "0xA", "pnl": 50000, "pseudonym": "A"},
            {"name": "0xB", "pnl": 40000, "pseudonym": "B"},
        ]
        recent_window = [{"name": "0xA"}, {"name": "0xB"}]

        async def stats_or_fail(session, entry):
            if entry["name"] == "0xB":
                raise RuntimeError("activity fetch failed")
            return make_stats(pnl_list=[10.0, 12.0, 11.0, 13.0])

        with (
            patch.object(
                client,
                "_fetch_dual_leaderboards",
                new=AsyncMock(return_value=(all_window, recent_window)),
            ),
            patch.object(
                client,
                "_build_trader_stats",
                new=AsyncMock(side_effect=stats_or_fail),
            ),
        ):
            result = await client.refresh()
        # 0xB's exception is swallowed (gather return_exceptions); 0xA still ranked.
        assert len(result) == 1


class TestTrackerAccessors:
    def test_top_wallet_addresses_empty_before_refresh(self):
        client = TrackerClient()
        assert client.top_wallet_addresses() == []

    def test_last_refresh_zero_before_refresh(self):
        assert TrackerClient().last_refresh() == 0.0

    def test_needs_rebalance_true_when_never_refreshed(self):
        # _last_refresh defaults to 0.0, so the elapsed interval is enormous.
        assert TrackerClient().needs_rebalance is True


class TestWorthlessExpiryImputation:
    """M13: open buys older than threshold are imputed as −100% losses."""

    @staticmethod
    def _buy(market="m", asset="a", price="0.50", size="100", ts=1_700_000_000):
        return {
            "id": "b1",
            "type": "trade",
            "side": "BUY",
            "market": market,
            "asset": asset,
            "price": price,
            "size": size,
            "timestamp": ts,
        }

    def test_old_open_buy_imputed_as_loss(self):
        # Buy timestamp is 60 days old (> 30d threshold) → imputed as −100%.
        old_ts = 1_700_000_000
        activity = [self._buy(ts=old_ts)]
        stats = _compute_trader_stats("0xabc", "Name", 50000, activity, impute_loss_after_days=30.0)
        assert stats.trade_count == 1
        assert stats.win_rate == 0.0
        assert stats.pnl_per_trade == pytest.approx([-1.0])

    def test_recent_open_buy_not_imputed(self):
        # Buy timestamp is 5 days old (< 30d threshold) → still open, not counted.
        import time as _time

        recent_ts = _time.time() - 5 * 86_400
        activity = [self._buy(ts=recent_ts)]
        stats = _compute_trader_stats("0xabc", "Name", 50000, activity, impute_loss_after_days=30.0)
        assert stats.pnl_per_trade == []

    def test_imputation_disabled_when_zero(self):
        # impute_loss_after_days=0 → open buys never imputed regardless of age.
        activity = [self._buy(ts=1_700_000_000)]
        stats = _compute_trader_stats("0xabc", "Name", 50000, activity, impute_loss_after_days=0.0)
        assert stats.pnl_per_trade == []

    def test_imputation_lowers_win_rate_vs_winning_sell(self):
        # One win + one old open buy → win_rate drops from 1.0 to 0.5.
        activity = [
            self._buy(market="m1", asset="a1", ts=1_700_000_000),
            {
                "id": "s1",
                "type": "trade",
                "side": "SELL",
                "market": "m1",
                "asset": "a1",
                "price": "0.70",
                "size": "70",
                "timestamp": 1_700_001_000,
            },
            self._buy(market="m2", asset="a2", ts=1_700_000_500),
        ]
        stats = _compute_trader_stats("0xabc", "Name", 50000, activity, impute_loss_after_days=30.0)
        assert stats.trade_count == 2
        assert stats.win_rate == pytest.approx(0.5)

    def test_multiple_open_buys_same_key_all_imputed(self):
        # Two open buys on same (market, asset) — both should be imputed.
        activity = [
            self._buy(ts=1_700_000_000),
            self._buy(ts=1_700_001_000),
        ]
        stats = _compute_trader_stats("0xabc", "Name", 50000, activity, impute_loss_after_days=30.0)
        assert stats.trade_count == 2
        assert stats.pnl_per_trade == pytest.approx([-1.0, -1.0])


class TestFrequencyNormalization:
    """M13: trades_per_week computed; freq_multiplier applied in scoring."""

    @staticmethod
    def _make_stats(**kwargs) -> TraderStats:
        defaults = dict(
            address="0xabc",
            pseudonym="",
            total_pnl=50_000.0,
            trade_count=100,
            win_rate=0.60,
            pnl_per_trade=[0.10] * 100,
            last_trade_time=1_700_000_000.0,
            trades_per_week=0.0,
        )
        defaults.update(kwargs)
        return TraderStats(**defaults)

    def test_trades_per_week_computed(self):
        # 4 trades over 28 days → 1 trade/week.
        base_ts = 1_700_000_000
        week = 7 * 86_400
        activity = []
        for i in range(4):
            activity.append(
                {
                    "id": f"b{i}",
                    "type": "trade",
                    "side": "BUY",
                    "market": f"m{i}",
                    "asset": f"a{i}",
                    "price": "0.50",
                    "size": "100",
                    "timestamp": base_ts + i * week,
                }
            )
            activity.append(
                {
                    "id": f"s{i}",
                    "type": "trade",
                    "side": "SELL",
                    "market": f"m{i}",
                    "asset": f"a{i}",
                    "price": "0.70",
                    "size": "70",
                    "timestamp": base_ts + i * week + 3600,
                }
            )
        stats = _compute_trader_stats("0xabc", "Name", 50000, activity, impute_loss_after_days=0.0)
        # 4 trades over 3 weeks (first ts to last ts) → ~1.33 trades/week
        assert stats.trades_per_week == pytest.approx(4 / 3.0, rel=0.01)

    def test_single_trade_has_zero_trades_per_week(self):
        # Cannot compute span from one timestamp → 0.0.
        activity = [
            {
                "id": "b1",
                "type": "trade",
                "side": "BUY",
                "market": "m",
                "asset": "a",
                "price": "0.50",
                "size": "100",
                "timestamp": 1_700_000_000,
            },
            {
                "id": "s1",
                "type": "trade",
                "side": "SELL",
                "market": "m",
                "asset": "a",
                "price": "0.70",
                "size": "70",
                "timestamp": 1_700_000_000,
            },
        ]
        stats = _compute_trader_stats("0xabc", "Name", 50000, activity, impute_loss_after_days=0.0)
        # Both at same timestamp → span = 0 → trades_per_week = 0.
        assert stats.trades_per_week == 0.0

    def test_freq_multiplier_scales_score(self):
        # A trader with trades_per_week=4 should score higher than one with 0.25
        # when all other stats are identical.
        cfg = TrackerConfig(
            min_total_pnl=0.0,
            min_trade_count=1,
            min_expectancy=0.0,
            freq_scale_cap=5.0,
        )
        scorer = TraderScorer(cfg)

        high_freq = self._make_stats(trades_per_week=4.0)
        low_freq = self._make_stats(trades_per_week=0.25)

        result_high = scorer.score(high_freq)
        result_low = scorer.score(low_freq)

        assert result_high is not None and result_low is not None
        assert result_high.score > result_low.score

    def test_zero_trades_per_week_uses_multiplier_one(self):
        # trades_per_week=0 → multiplier=1.0 → same result as baseline.
        cfg = TrackerConfig(min_total_pnl=0.0, min_trade_count=1, min_expectancy=0.0)
        scorer = TraderScorer(cfg)

        stats_zero = self._make_stats(trades_per_week=0.0)
        stats_one_per_week = self._make_stats(trades_per_week=1.0)  # sqrt(1)=1 → same mult

        r_zero = scorer.score(stats_zero)
        r_one = scorer.score(stats_one_per_week)
        assert r_zero is not None and r_one is not None
        assert r_zero.score == pytest.approx(r_one.score)

    def test_freq_scale_cap_limits_multiplier(self):
        # trades_per_week=100 → sqrt=10, but cap=3 → multiplier stays at 3.
        cfg = TrackerConfig(min_total_pnl=0.0, min_trade_count=1, min_expectancy=0.0, freq_scale_cap=3.0)
        scorer = TraderScorer(cfg)
        stats_high = self._make_stats(trades_per_week=100.0)
        stats_cap = self._make_stats(trades_per_week=9.0)  # sqrt(9)=3 = cap exactly

        r_high = scorer.score(stats_high)
        r_cap = scorer.score(stats_cap)
        assert r_high is not None and r_cap is not None
        assert r_high.score == pytest.approx(r_cap.score)


class TestDiversifiedSelection:
    """L7: the final top-N is diversified by market overlap so it isn't all the
    same correlated favorite-buyers."""

    @staticmethod
    def _stats(addr, markets, mean=0.10):
        # A clean, eligible trader with a fixed per-trade ROI and given markets.
        return TraderStats(
            address=addr,
            pseudonym=addr,
            total_pnl=50_000.0,
            trade_count=100,
            win_rate=0.60,
            pnl_per_trade=[mean] * 100,
            last_trade_time=time.time(),
            markets=frozenset(markets),
        )

    def _scorer(self, **overrides):
        params = dict(
            min_total_pnl=0.0,
            min_trade_count=1,
            min_expectancy=0.0,
            max_top_traders=2,
            diversify_enabled=True,
            overlap_penalty=0.5,
        )
        params.update(overrides)
        return TraderScorer(TrackerConfig(**params))

    def test_overlap_penalty_demotes_correlated_trader(self):
        # A and B trade the SAME markets (high score, but redundant); C trades a
        # disjoint set at a slightly lower score. Diversification should pick A then
        # C (not B), trading a little score for independence.
        scorer = self._scorer()
        a = self._stats("A", {"m1", "m2", "m3"}, mean=0.20)  # top score
        b = self._stats("B", {"m1", "m2", "m3"}, mean=0.19)  # nearly identical book
        c = self._stats("C", {"m9", "m8", "m7"}, mean=0.18)  # disjoint book
        top = scorer.score_many([a, b, c])
        assert [t.stats.address for t in top] == ["A", "C"]

    def test_no_overlap_keeps_pure_score_order(self):
        # All disjoint markets → no penalty → ranking is pure score order.
        scorer = self._scorer()
        a = self._stats("A", {"m1"}, mean=0.20)
        b = self._stats("B", {"m2"}, mean=0.19)
        c = self._stats("C", {"m3"}, mean=0.05)
        top = scorer.score_many([a, b, c])
        assert [t.stats.address for t in top] == ["A", "B"]

    def test_disabled_uses_pure_score_order(self):
        # With diversify disabled, the two highest scorers win even if identical.
        scorer = self._scorer(diversify_enabled=False)
        a = self._stats("A", {"m1", "m2"}, mean=0.20)
        b = self._stats("B", {"m1", "m2"}, mean=0.19)
        c = self._stats("C", {"m9"}, mean=0.18)
        top = scorer.score_many([a, b, c])
        assert [t.stats.address for t in top] == ["A", "B"]

    def test_top_scorer_always_picked_first(self):
        scorer = self._scorer()
        a = self._stats("A", {"m1", "m2", "m3"}, mean=0.30)
        b = self._stats("B", {"m4"}, mean=0.10)
        c = self._stats("C", {"m5"}, mean=0.10)
        top = scorer.score_many([a, b, c])
        assert top[0].stats.address == "A"
        assert top[0].rank == 1

    def test_empty_markets_no_penalty(self):
        # Traders with no market data can't be assessed for overlap → score order.
        scorer = self._scorer()
        a = self._stats("A", set(), mean=0.20)
        b = self._stats("B", set(), mean=0.19)
        c = self._stats("C", set(), mean=0.05)
        top = scorer.score_many([a, b, c])
        assert [t.stats.address for t in top] == ["A", "B"]


class TestMarketsTracked:
    """L7: _compute_trader_stats records the distinct markets a trader touched."""

    def test_markets_collected_from_activity(self):
        activity = [
            {
                "id": "b1",
                "type": "trade",
                "side": "BUY",
                "market": "m1",
                "asset": "a1",
                "price": "0.50",
                "size": "100",
                "timestamp": 1_700_000_000,
            },
            {
                "id": "s1",
                "type": "trade",
                "side": "SELL",
                "market": "m1",
                "asset": "a1",
                "price": "0.60",
                "size": "60",
                "timestamp": 1_700_001_000,
            },
            {
                "id": "b2",
                "type": "trade",
                "side": "BUY",
                "market": "m2",
                "asset": "a2",
                "price": "0.20",
                "size": "20",
                "timestamp": 1_700_000_500,
            },
            {
                "id": "r2",
                "type": "redeem",
                "market": "m2",
                "asset": "a2",
                "price": "1.0",
                "size": "100",
                "timestamp": 1_700_003_000,
            },
        ]
        stats = _compute_trader_stats("0xabc", "Name", 50000, activity)
        assert stats.markets == frozenset({"m1", "m2"})


class TestWashTradeGuard:
    """M11: self-cross (maker == taker) activity records are excluded from scoring."""

    def _make_activity(self, buy_maker="0xwhale", buy_taker="0xother", sell_maker="0xother", sell_taker="0xwhale"):
        return [
            {
                "type": "trade",
                "side": "BUY",
                "market": "m1",
                "asset": "tok1",
                "price": 0.50,
                "size": 100,
                "timestamp": 1_700_000_000,
                "maker": buy_maker,
                "taker": buy_taker,
            },
            {
                "type": "trade",
                "side": "SELL",
                "market": "m1",
                "asset": "tok1",
                "price": 0.60,
                "size": 100,
                "timestamp": 1_700_001_000,
                "maker": sell_maker,
                "taker": sell_taker,
            },
        ]

    def test_normal_trade_included(self):
        activity = self._make_activity()
        stats = _compute_trader_stats("0xwhale", "W", 10000, activity)
        assert stats.trade_count == 1  # one completed round-trip
        assert stats.win_rate == 1.0

    def test_self_cross_buy_excluded(self):
        # BUY is a self-cross → no open position → SELL finds no match → no scored trades
        activity = self._make_activity(buy_maker="0xwhale", buy_taker="0xwhale")
        stats = _compute_trader_stats("0xwhale", "W", 10000, activity)
        assert stats.pnl_per_trade == []  # no completed round-trips scored

    def test_self_cross_sell_excluded(self):
        # Normal BUY opens; self-cross SELL is skipped → open buy imputed as loss
        activity = self._make_activity(sell_maker="0xwhale", sell_taker="0xwhale")
        stats = _compute_trader_stats("0xwhale", "W", 10000, activity, impute_loss_after_days=0.0001)
        # BUY was normal, SELL self-cross skipped; old open buy imputed as -100% loss
        assert len(stats.pnl_per_trade) == 1
        assert stats.win_rate == 0.0

    def test_both_self_cross_excluded(self):
        activity = self._make_activity(
            buy_maker="0xwhale", buy_taker="0xwhale", sell_maker="0xwhale", sell_taker="0xwhale"
        )
        stats = _compute_trader_stats("0xwhale", "W", 10000, activity)
        assert stats.pnl_per_trade == []

    def test_missing_maker_taker_not_filtered(self):
        # Records without maker/taker fields (older API responses) should not be filtered.
        activity = [
            {
                "type": "trade",
                "side": "BUY",
                "market": "m1",
                "asset": "tok1",
                "price": 0.50,
                "size": 100,
                "timestamp": 1_700_000_000,
            },
            {
                "type": "trade",
                "side": "SELL",
                "market": "m1",
                "asset": "tok1",
                "price": 0.60,
                "size": 100,
                "timestamp": 1_700_001_000,
            },
        ]
        stats = _compute_trader_stats("0xwhale", "W", 10000, activity)
        assert stats.trade_count == 1

    def test_case_insensitive_address_comparison(self):
        # Same address differing only in case should still be caught as self-cross.
        activity = self._make_activity(buy_maker="0xWHALE", buy_taker="0xwhale")
        stats = _compute_trader_stats("0xwhale", "W", 10000, activity)
        assert stats.pnl_per_trade == []

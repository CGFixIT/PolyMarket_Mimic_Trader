"""
core/tracker.py — Risk-adjusted top trader discovery and scoring.

WHY NOT JUST USE LEADERBOARD PnL RANK?
----------------------------------------
Raw PnL on the Polymarket leaderboard rewards CONCENTRATION and LUCK.
A trader who bet 80% of bankroll on three markets and won all three will
show higher PnL than a disciplined trader who spread risk across 200 markets
at 60% win rate. The concentrated bettor is likely to blow up eventually.

SCORING FORMULA
---------------
Three independent axes multiplied together:

  Score = Sharpe_proxy × Consistency × Recency_weight

  Sharpe_proxy    = mean_pnl_per_trade / stddev_pnl_per_trade
  Consistency     = win_rate × log(trade_count + 1)
  Recency_weight  = exp(−λ × days_since_last_trade)
                    where λ = ln(2) / half_life_days

MINIMUM ELIGIBILITY THRESHOLDS (applied before scoring)
---------------------------------------------------------
  min_total_pnl   = $10,000   Filter out small accounts
  min_win_rate    = 0.55      At least 55% win rate
  min_trade_count = 50        Enough history to compute meaningful stats
"""

from __future__ import annotations

import asyncio
import logging
import math
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

logger = logging.getLogger(__name__)

_EPSILON             = 1e-9
POLYMARKET_DATA_API  = "https://data-api.polymarket.com"


# ─── Config ───────────────────────────────────────────────────────────────────

@dataclass
class TrackerConfig:
    # Eligibility filters (applied before scoring)
    min_total_pnl:       float = 10_000.0
    min_win_rate:        float = 0.55
    min_trade_count:     int   = 50

    # Scoring parameters
    half_life_days:      float = 14.0    # Recency decay: score halves every N days
    max_top_traders:     int   = 5       # Number of traders to return

    # Data fetch limits
    activity_fetch_limit: int  = 500     # Trades to pull per trader for stats
    leaderboard_limit:   int   = 50      # Candidates to fetch from leaderboard

    # Rebalance schedule
    rebalance_interval_days: float = 7.0


# ─── Data Models ──────────────────────────────────────────────────────────────

@dataclass
class TradeRecord:
    """A single resolved trade used for PnL statistics."""
    trade_id:    str
    market_id:   str
    pnl:         float   # Positive = profit, negative = loss
    is_win:      bool
    executed_at: float   # Unix timestamp


@dataclass
class TraderStats:
    """Full statistical profile of a trader derived from their activity."""
    address:         str
    pseudonym:       str
    total_pnl:       float
    trade_count:     int
    win_rate:        float
    pnl_per_trade:   List[float]    = field(default_factory=list)
    last_trade_time: float          = 0.0

    @property
    def mean_pnl(self) -> float:
        return statistics.mean(self.pnl_per_trade) if self.pnl_per_trade else 0.0

    @property
    def stddev_pnl(self) -> float:
        if len(self.pnl_per_trade) < 2:
            return 0.0
        return statistics.stdev(self.pnl_per_trade)

    @property
    def sharpe_proxy(self) -> float:
        """Mean PnL / StdDev PnL. Undefined if no variance (constant returns)."""
        denom = self.stddev_pnl
        if denom < _EPSILON:
            return self.mean_pnl / _EPSILON if self.mean_pnl > 0 else 0.0
        return self.mean_pnl / denom


@dataclass
class ScoredTrader:
    """A trader with their composite score and score component breakdown."""
    stats:           TraderStats
    score:           float
    sharpe_proxy:    float
    consistency:     float
    recency_weight:  float
    rank:            int = 0

    def __repr__(self) -> str:
        return (
            f"ScoredTrader(addr={self.stats.address[:10]}... "
            f"score={self.score:.4f} sharpe={self.sharpe_proxy:.3f} "
            f"consistency={self.consistency:.3f} recency={self.recency_weight:.3f} "
            f"win_rate={self.stats.win_rate:.1%} trades={self.stats.trade_count})"
        )


# ─── Scorer ───────────────────────────────────────────────────────────────────

class TraderScorer:
    """
    Stateless scoring logic. Separated from TrackerClient so it can be
    tested in isolation without live API calls.
    """

    def __init__(self, config: TrackerConfig):
        self.cfg = config

    def score(self, stats: TraderStats) -> Optional[ScoredTrader]:
        """
        Compute composite score for a trader. Returns None if the trader
        fails minimum eligibility thresholds.
        """
        if not self._is_eligible(stats):
            return None

        sharpe      = stats.sharpe_proxy
        consistency = stats.win_rate * math.log(stats.trade_count + 1)
        recency     = self._recency_weight(stats.last_trade_time)

        score = sharpe * consistency * recency

        return ScoredTrader(
            stats          = stats,
            score          = score,
            sharpe_proxy   = sharpe,
            consistency    = consistency,
            recency_weight = recency,
        )

    def score_many(self, all_stats: List[TraderStats]) -> List[ScoredTrader]:
        """
        Score and rank a list of traders. Returns top N by score,
        descending, with rank assigned (1 = best).
        """
        scored = []
        for s in all_stats:
            result = self.score(s)
            if result is not None:
                scored.append(result)

        scored.sort(key=lambda x: x.score, reverse=True)

        top = scored[: self.cfg.max_top_traders]
        for i, trader in enumerate(top):
            trader.rank = i + 1

        return top

    def _is_eligible(self, stats: TraderStats) -> bool:
        reasons = []
        if stats.total_pnl < self.cfg.min_total_pnl:
            reasons.append(f"total_pnl={stats.total_pnl:.0f} < {self.cfg.min_total_pnl:.0f}")
        if stats.win_rate < self.cfg.min_win_rate:
            reasons.append(f"win_rate={stats.win_rate:.2%} < {self.cfg.min_win_rate:.2%}")
        if stats.trade_count < self.cfg.min_trade_count:
            reasons.append(f"trade_count={stats.trade_count} < {self.cfg.min_trade_count}")

        if reasons:
            logger.debug(
                "Trader %s ineligible: %s",
                stats.address[:10], "; ".join(reasons)
            )
            return False
        return True

    def _recency_weight(self, last_trade_time: float) -> float:
        """
        Exponential decay: weight = exp(−λ × days_since_last_trade)
        λ = ln(2) / half_life_days  →  weight halves every half_life_days.
        """
        if last_trade_time <= 0:
            return 0.0

        days_inactive = (time.time() - last_trade_time) / 86_400.0
        days_inactive = max(days_inactive, 0.0)   # Guard against clock skew

        lambda_decay = math.log(2) / self.cfg.half_life_days
        return math.exp(-lambda_decay * days_inactive)


# ─── TrackerClient ────────────────────────────────────────────────────────────

class TrackerClient:
    """
    Fetches trader data from the Polymarket Data API and produces
    scored+ranked trader lists.

    Call refresh() on startup and then on a rebalance schedule.
    """

    def __init__(
        self,
        config:      Optional[TrackerConfig] = None,
        data_api:    str                     = POLYMARKET_DATA_API,
    ):
        self.cfg         = config or TrackerConfig()
        self._data_api   = data_api
        self._scorer     = TraderScorer(self.cfg)
        self.top_traders: List[ScoredTrader] = []
        self._last_refresh: float = 0.0

    async def refresh(self) -> List[ScoredTrader]:
        """
        Full pipeline: leaderboard → per-trader stats → scoring → ranking.
        Caches results in self.top_traders.
        """
        async with aiohttp.ClientSession(
            headers={"User-Agent": "polymarket-copier/1.0"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as session:
            candidates = await self._fetch_leaderboard(session)

            if not candidates:
                logger.warning("Leaderboard returned no candidates.")
                return []

            logger.info("Fetching activity for %d leaderboard candidates.", len(candidates))

            stats_tasks = [
                self._build_trader_stats(session, entry)
                for entry in candidates
            ]
            all_stats_raw = await asyncio.gather(*stats_tasks, return_exceptions=True)

            all_stats: List[TraderStats] = []
            for entry, result in zip(candidates, all_stats_raw):
                if isinstance(result, Exception):
                    logger.warning(
                        "Stats fetch failed for %s: %s",
                        entry.get("address", "?")[:10], result
                    )
                elif isinstance(result, TraderStats):
                    all_stats.append(result)

            self.top_traders   = self._scorer.score_many(all_stats)
            self._last_refresh = time.time()

            for t in self.top_traders:
                logger.info(
                    "Rank #%d | %s | score=%.4f | win_rate=%.1f%% | "
                    "trades=%d | sharpe=%.3f | recency=%.3f",
                    t.rank,
                    t.stats.pseudonym or t.stats.address[:12],
                    t.score,
                    t.stats.win_rate * 100,
                    t.stats.trade_count,
                    t.sharpe_proxy,
                    t.recency_weight,
                )

            return self.top_traders

    @property
    def needs_rebalance(self) -> bool:
        elapsed_days = (time.time() - self._last_refresh) / 86_400.0
        return elapsed_days >= self.cfg.rebalance_interval_days

    def top_wallet_addresses(self) -> List[str]:
        return [t.stats.address for t in self.top_traders]

    # ── Private: API Fetchers ─────────────────────────────────────────────────

    async def _fetch_leaderboard(
        self, session: aiohttp.ClientSession
    ) -> List[dict]:
        """
        Fetch the Polymarket leaderboard sorted by all-time PnL.
        API: GET /leaderboard?window=all&limit=N
        """
        url    = f"{self._data_api}/leaderboard"
        params: Dict[str, Any] = {"window": "all", "limit": self.cfg.leaderboard_limit}

        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.error("Leaderboard fetch returned HTTP %d", resp.status)
                    return []
                data = await resp.json()
                # Pre-filter by minimum PnL before paying for per-trader API calls
                return [
                    entry for entry in data
                    if float(entry.get("pnl", 0)) >= self.cfg.min_total_pnl
                ]
        except Exception as exc:
            logger.error("Leaderboard fetch failed: %s", exc)
            return []

    async def _build_trader_stats(
        self,
        session: aiohttp.ClientSession,
        leaderboard_entry: dict,
    ) -> Optional[TraderStats]:
        """
        Fetch a trader's recent activity and compute TraderStats.
        Combines leaderboard aggregate data with per-trade detail from /activity.
        """
        address   = leaderboard_entry.get("name", "")      # "name" = wallet address
        pseudonym = leaderboard_entry.get("pseudonym", "")
        total_pnl = float(leaderboard_entry.get("pnl", 0))

        if not address:
            return None

        trades = await self._fetch_activity(session, address)

        if not trades:
            trade_count = int(leaderboard_entry.get("tradesCount", 0))
            return TraderStats(
                address         = address,
                pseudonym       = pseudonym,
                total_pnl       = total_pnl,
                trade_count     = trade_count,
                win_rate        = 0.0,
                pnl_per_trade   = [],
                last_trade_time = 0.0,
            )

        return _compute_trader_stats(address, pseudonym, total_pnl, trades)

    async def _fetch_activity(
        self,
        session: aiohttp.ClientSession,
        address: str,
    ) -> List[dict]:
        """
        Fetch recent trade activity for a wallet.
        API: GET /activity?user={address}&limit=N
        """
        url    = f"{self._data_api}/activity"
        params: Dict[str, Any] = {"user": address, "limit": self.cfg.activity_fetch_limit}

        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.warning(
                        "Activity fetch for %s returned HTTP %d",
                        address[:10], resp.status
                    )
                    return []
                return await resp.json()
        except Exception as exc:
            logger.warning("Activity fetch failed for %s: %s", address[:10], exc)
            return []


# ─── Stats Computation ────────────────────────────────────────────────────────

def _compute_trader_stats(
    address:   str,
    pseudonym: str,
    total_pnl: float,
    activity:  List[dict],
) -> TraderStats:
    """
    Derive TraderStats from raw activity records.

    PnL per trade is estimated from (exit_price − entry_price) × size for
    completed round-trips. Partial data is excluded rather than estimated.
    """
    trade_records: List[TradeRecord] = []
    last_trade_ts = 0.0

    # Pair BUY and SELL events for the same market/token to estimate PnL.
    open_buys: Dict[Tuple[str, str], List[dict]] = {}

    for item in activity:
        if item.get("type", "").lower() not in ("trade", "buy", "sell"):
            continue

        market_id = str(item.get("market", item.get("conditionId", "")))
        token_id  = str(item.get("asset",  item.get("tokenId",    "")))
        side      = str(item.get("side",   "")).upper()

        try:
            price = float(item.get("price", 0))
            size  = float(item.get("size",  item.get("usdcSize", 0)))
        except (ValueError, TypeError):
            continue

        ts_raw = item.get("timestamp", item.get("createdAt", 0))
        ts     = _parse_timestamp(ts_raw)
        last_trade_ts = max(last_trade_ts, ts)

        key = (market_id, token_id)

        if side == "BUY":
            if key not in open_buys:
                open_buys[key] = []
            open_buys[key].append({"price": price, "size": size, "ts": ts})

        elif side == "SELL" and key in open_buys and open_buys[key]:
            buy = open_buys[key].pop(0)  # FIFO matching
            pnl = (price - buy["price"]) * (size / max(price, _EPSILON))
            trade_records.append(TradeRecord(
                trade_id    = str(item.get("id", "")),
                market_id   = market_id,
                pnl         = pnl,
                is_win      = pnl > 0,
                executed_at = ts,
            ))

    if not trade_records:
        return TraderStats(
            address         = address,
            pseudonym       = pseudonym,
            total_pnl       = total_pnl,
            trade_count     = len(activity),
            win_rate        = 0.0,
            pnl_per_trade   = [],
            last_trade_time = last_trade_ts,
        )

    wins          = sum(1 for t in trade_records if t.is_win)
    win_rate      = wins / len(trade_records)
    pnl_per_trade = [t.pnl for t in trade_records]

    return TraderStats(
        address         = address,
        pseudonym       = pseudonym,
        total_pnl       = total_pnl,
        trade_count     = len(trade_records),
        win_rate        = win_rate,
        pnl_per_trade   = pnl_per_trade,
        last_trade_time = last_trade_ts,
    )


def _parse_timestamp(raw) -> float:
    """Parse ISO 8601 string or numeric timestamp to Unix float."""
    if isinstance(raw, str):
        try:
            from datetime import datetime
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    elif isinstance(raw, (int, float)):
        # If in milliseconds (> year 3000 as seconds ≈ 3.2e10)
        return float(raw) / 1_000.0 if raw > 1e12 else float(raw)
    return 0.0

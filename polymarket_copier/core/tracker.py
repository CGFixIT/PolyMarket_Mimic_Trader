"""
core/tracker.py — Risk-adjusted top trader discovery and scoring.

WHY NOT JUST USE LEADERBOARD PnL RANK?
----------------------------------------
Raw PnL on the Polymarket leaderboard rewards CONCENTRATION and LUCK.
A trader who bet 80% of bankroll on three markets and won all three will
show higher PnL than a disciplined trader who spread risk across 200 markets
at 60% win rate. The concentrated bettor is likely to blow up eventually.

SCORING FORMULA (H14: Weighted Sum)
-----------------------------------
Three independent axes weighted together (not multiplied, to prevent extreme
values from dominating):

  Score = 0.40×Sharpe_proxy + 0.35×Consistency + 0.25×Recency_weight

  Sharpe_proxy    = min(mean_pnl_per_trade / stddev_pnl_per_trade, sharpe_cap)
                    capped at 3.0; shrunk for small samples (<20 trades)
  Consistency     = win_rate × log(trade_count + 1)
  Recency_weight  = exp(−λ × days_since_last_trade)
                    where λ = ln(2) / half_life_days

MINIMUM ELIGIBILITY THRESHOLDS (applied before scoring)
---------------------------------------------------------
  min_total_pnl   = $10,000      Filter out small accounts
  min_expectancy  = 0.01         Minimum edge: mean_pnl × log(n+1) ≥ 1%
                                 (H16: replaces win_rate check)
  min_trade_count = 50           Enough history to compute meaningful stats

DUAL-WINDOW FILTERING (H15)
---------------------------
  Require traders to rank in BOTH all-time and recent (30d) leaderboards
  to filter out lucky past streaks that aren't repeating.
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

_EPSILON = 1e-9
POLYMARKET_DATA_API = "https://data-api.polymarket.com"


# ─── Config ───────────────────────────────────────────────────────────────────


@dataclass
class TrackerConfig:
    # Eligibility filters (applied before scoring)
    min_total_pnl: float = 10_000.0
    min_win_rate: float = 0.55
    min_trade_count: int = 50
    min_expectancy: float = 0.01  # H16: min expected ROI (mean_pnl × log(n+1))

    # Scoring parameters
    # L4: dropped from 14 → 7 days. A 14-day half-life let a trader who went
    # dormant 4 weeks ago retain 25% of their recency score; 7 days drops that
    # to 6%, properly down-weighting inactivity.
    half_life_days: float = 7.0  # Recency decay: score halves every N days
    max_top_traders: int = 5  # Number of traders to return
    sharpe_cap: float = 3.0  # H14: cap Sharpe to prevent outlier amplification
    sharpe_shrink_min_trades: int = 20  # H14: shrink Sharpe below this sample size

    # Data fetch limits
    activity_fetch_limit: int = 500  # Trades to pull per trader for stats
    leaderboard_limit: int = 50  # Candidates to fetch from leaderboard
    recent_window_days: int = 30  # H15: trailing window for dual-window filtering

    # Rebalance schedule
    rebalance_interval_days: float = 7.0


# ─── Data Models ──────────────────────────────────────────────────────────────


@dataclass
class TradeRecord:
    """A single resolved trade used for return statistics."""

    trade_id: str
    market_id: str
    pnl: float  # ROI fraction (pnl_dollars / cost_basis); +0.10 = +10%
    is_win: bool
    executed_at: float  # Unix timestamp


@dataclass
class TraderStats:
    """Full statistical profile of a trader derived from their activity."""

    address: str
    pseudonym: str
    total_pnl: float  # Leaderboard aggregate PnL, in DOLLARS
    trade_count: int
    win_rate: float
    # Per round-trip RETURN ON CAPITAL (ROI fractions, not dollars). This keeps
    # the Sharpe proxy / mean / stddev size-independent. total_pnl above stays
    # in dollars for the min_total_pnl eligibility filter.
    pnl_per_trade: List[float] = field(default_factory=list)
    last_trade_time: float = 0.0

    @property
    def mean_pnl(self) -> float:
        """Mean per-trade ROI fraction (e.g. 0.10 = +10% average return)."""
        return statistics.mean(self.pnl_per_trade) if self.pnl_per_trade else 0.0

    @property
    def stddev_pnl(self) -> float:
        """Std-dev of per-trade ROI fractions (return volatility)."""
        if len(self.pnl_per_trade) < 2:
            return 0.0
        return statistics.stdev(self.pnl_per_trade)

    @property
    def sharpe_proxy(self) -> float:
        """Mean return / StdDev return. Undefined if no variance (constant returns)."""
        denom = self.stddev_pnl
        if denom < _EPSILON:
            return self.mean_pnl / _EPSILON if self.mean_pnl > 0 else 0.0
        return self.mean_pnl / denom

    @property
    def expectancy(self) -> float:
        """Expected profit per trade weighted by sample size: mean_pnl × log(n+1)."""
        return self.mean_pnl * math.log(max(self.trade_count, 1) + 1)


@dataclass
class ScoredTrader:
    """A trader with their composite score and score component breakdown."""

    stats: TraderStats
    score: float
    sharpe_proxy: float
    consistency: float
    recency_weight: float
    rank: int = 0

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

        # H14: cap Sharpe to prevent outliers; shrink for small samples.
        sharpe = self._capped_sharpe(stats)
        consistency = stats.win_rate * math.log(stats.trade_count + 1)
        recency = self._recency_weight(stats.last_trade_time)

        # H14: weighted sum instead of multiplication to prevent single extreme
        # component from dominating. Weights: sharpe (40%) + consistency (35%) + recency (25%).
        score = (4.0 * sharpe + 3.5 * consistency + 2.5 * recency) / 10.0

        return ScoredTrader(
            stats=stats,
            score=score,
            sharpe_proxy=sharpe,
            consistency=consistency,
            recency_weight=recency,
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
        """Return True if the trader passes the min PnL, trade-count, and expectancy thresholds."""
        reasons = []
        if stats.total_pnl < self.cfg.min_total_pnl:
            reasons.append(f"total_pnl={stats.total_pnl:.0f} < {self.cfg.min_total_pnl:.0f}")
        if stats.trade_count < self.cfg.min_trade_count:
            reasons.append(f"trade_count={stats.trade_count} < {self.cfg.min_trade_count}")
        # H16: gate on expectancy instead of win_rate (size-independent edge metric)
        if stats.expectancy < self.cfg.min_expectancy:
            reasons.append(f"expectancy={stats.expectancy:.4f} < {self.cfg.min_expectancy:.4f}")
        # H16: soft check on win_rate (log warning but don't hard-fail)
        if stats.win_rate < self.cfg.min_win_rate:
            logger.debug(
                "Trader %s has low win_rate=%.2f%% (below %.2f%%) but passes expectancy check",
                stats.address[:10],
                stats.win_rate * 100,
                self.cfg.min_win_rate * 100,
            )

        if reasons:
            logger.debug("Trader %s ineligible: %s", stats.address[:10], "; ".join(reasons))
            return False
        return True

    def _capped_sharpe(self, stats: TraderStats) -> float:
        """
        H14: cap Sharpe and shrink for small samples to prevent lucky streaks
        from inflating the score.

        For trades < sharpe_shrink_min_trades, scale Sharpe down by the ratio
        of trade_count to min_threshold (e.g., 10 trades vs 20 threshold → 0.5x).
        Then cap at sharpe_cap to prevent unbounded extreme values.
        """
        sharpe = stats.sharpe_proxy

        # Shrink sharpe toward zero for small samples
        if stats.trade_count < self.cfg.sharpe_shrink_min_trades:
            shrink_factor = stats.trade_count / self.cfg.sharpe_shrink_min_trades
            sharpe = sharpe * shrink_factor

        # Cap at maximum to prevent one extreme component from dominating
        return min(sharpe, self.cfg.sharpe_cap)

    def _recency_weight(self, last_trade_time: float) -> float:
        """
        Exponential decay: weight = exp(−λ × days_since_last_trade)
        λ = ln(2) / half_life_days  →  weight halves every half_life_days.
        """
        if last_trade_time <= 0:
            return 0.0

        days_inactive = (time.time() - last_trade_time) / 86_400.0
        days_inactive = max(days_inactive, 0.0)  # Guard against clock skew

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
        config: Optional[TrackerConfig] = None,
        data_api: str = POLYMARKET_DATA_API,
    ):
        self.cfg = config or TrackerConfig()
        self._data_api = data_api
        self._scorer = TraderScorer(self.cfg)
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
            # H15: fetch both all-time and recent windows; require traders to rank in both
            all_window, recent_window = await self._fetch_dual_leaderboards(session)

            if not all_window or not recent_window:
                logger.warning("Could not fetch both leaderboard windows.")
                return []

            # Build set of traders in both windows (H15: dual-window consistency filter)
            recent_addrs = {e.get("name", "") for e in recent_window}
            candidates = [e for e in all_window if e.get("name", "") in recent_addrs]

            if not candidates:
                logger.warning("No traders found in both all-time and recent windows.")
                return []

            logger.info(
                "Fetching activity for %d candidates in both windows (all_count=%d, recent_count=%d).",
                len(candidates),
                len(all_window),
                len(recent_window),
            )

            stats_tasks = [self._build_trader_stats(session, entry) for entry in candidates]
            all_stats_raw = await asyncio.gather(*stats_tasks, return_exceptions=True)

            all_stats: List[TraderStats] = []
            for entry, result in zip(candidates, all_stats_raw, strict=True):
                if isinstance(result, Exception):
                    logger.warning("Stats fetch failed for %s: %s", entry.get("address", "?")[:10], result)
                elif isinstance(result, TraderStats):
                    all_stats.append(result)

            self.top_traders = self._scorer.score_many(all_stats)
            self._last_refresh = time.time()

            for t in self.top_traders:
                logger.info(
                    "Rank #%d | %s | score=%.4f | expectancy=%.4f | trades=%d | sharpe=%.3f | recency=%.3f",
                    t.rank,
                    t.stats.pseudonym or t.stats.address[:12],
                    t.score,
                    t.stats.expectancy,
                    t.stats.trade_count,
                    t.sharpe_proxy,
                    t.recency_weight,
                )

            return self.top_traders

    @property
    def needs_rebalance(self) -> bool:
        """Whether the rebalance interval has elapsed since the last refresh()."""
        elapsed_days = (time.time() - self._last_refresh) / 86_400.0
        return elapsed_days >= self.cfg.rebalance_interval_days

    def top_wallet_addresses(self) -> List[str]:
        """Return the wallet addresses of the currently ranked top traders."""
        return [t.stats.address for t in self.top_traders]

    def last_refresh(self) -> float:
        """Unix timestamp of the last successful refresh() (0.0 if never run)."""
        return self._last_refresh

    # ── Private: API Fetchers ─────────────────────────────────────────────────

    async def _fetch_dual_leaderboards(self, session: aiohttp.ClientSession) -> Tuple[List[dict], List[dict]]:
        """
        H15: Fetch both all-time and recent (30d) leaderboards to filter for
        dual-window consistency. Returns (all_window, recent_window).
        """
        all_lb = await self._fetch_leaderboard_window(session, "all")
        recent_days = max(int(self.cfg.recent_window_days), 1)
        recent_window = f"{recent_days}d"
        recent_lb = await self._fetch_leaderboard_window(session, recent_window)
        return all_lb, recent_lb

    async def _fetch_leaderboard_window(self, session: aiohttp.ClientSession, window: str) -> List[dict]:
        """Fetch leaderboard for a specific time window (e.g., 'all', '30d')."""
        url = f"{self._data_api}/leaderboard"
        params: Dict[str, Any] = {"window": window, "limit": self.cfg.leaderboard_limit}

        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.warning("Leaderboard fetch (window=%s) returned HTTP %d", window, resp.status)
                    return []
                data = await resp.json()
                # Pre-filter by minimum PnL before paying for per-trader API calls
                return [entry for entry in data if float(entry.get("pnl", 0)) >= self.cfg.min_total_pnl]
        except Exception as exc:
            logger.warning("Leaderboard fetch (window=%s) failed: %s", window, exc)
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
        address = leaderboard_entry.get("name", "")  # "name" = wallet address
        pseudonym = leaderboard_entry.get("pseudonym", "")
        total_pnl = float(leaderboard_entry.get("pnl", 0))

        if not address:
            return None

        trades = await self._fetch_activity(session, address)

        if not trades:
            trade_count = int(leaderboard_entry.get("tradesCount", 0))
            return TraderStats(
                address=address,
                pseudonym=pseudonym,
                total_pnl=total_pnl,
                trade_count=trade_count,
                win_rate=0.0,
                pnl_per_trade=[],
                last_trade_time=0.0,
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
        url = f"{self._data_api}/activity"
        params: Dict[str, Any] = {"user": address, "limit": self.cfg.activity_fetch_limit}

        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.warning("Activity fetch for %s returned HTTP %d", address[:10], resp.status)
                    return []
                return await resp.json()
        except Exception as exc:
            logger.warning("Activity fetch failed for %s: %s", address[:10], exc)
            return []


# ─── Stats Computation ────────────────────────────────────────────────────────


def _compute_trader_stats(
    address: str,
    pseudonym: str,
    total_pnl: float,
    activity: List[dict],
) -> TraderStats:
    """
    Derive TraderStats from raw activity records.

    SCORING ON RETURN ON CAPITAL (ROI)
    -----------------------------------
    Per-trade values stored in pnl_per_trade are ROI fractions, not absolute dollars.
    For each completed round-trip:
        pnl_dollars = (exit_price − entry_price) × buy_shares
        cost_basis  = entry_price × buy_shares   (capital put at risk)
        roi         = pnl_dollars / cost_basis

    This makes the Sharpe proxy, mean, and stddev size-independent: a trader who
    consistently makes 5% per trade scores the same whether notional was $10 or
    $10,000 — key for a small-bankroll copy bot. total_pnl stays in dollars (the
    leaderboard aggregate used by min_total_pnl eligibility filter).

    RESOLUTION AWARENESS
    --------------------
    A common Polymarket alpha is buying a mispriced YES/NO token and HOLDING it to
    resolution — never SOLD, but REDEEMED when the market resolves (winning shares
    pay $1.00, losing shares pay $0.00). We treat redemption/claim records as realizing
    events so held-to-resolution outcomes are counted, not silently dropped.

    LIMITATION (honest accounting): we can only credit redemptions we OBSERVE in the
    activity feed. Winning positions emit a redeem/claim record (captured). Losing
    positions expiring worthless typically emit NO redeem record — nothing to redeem —
    so those losses remain uncounted. This biases observed win_rate UPWARD for traders
    who hold losers to worthless expiry. Monitor this when interpreting stats.

    Partial data is excluded rather than estimated.
    """
    trade_records: List[TradeRecord] = []
    last_trade_ts = 0.0

    # Pair BUY and SELL events for the same market/token to estimate PnL.
    open_buys: Dict[Tuple[str, str], List[dict]] = {}

    # Activity record types that REALIZE an open position by paying it out at
    # resolution (vs. selling it on the order book). Matched case-insensitively
    # against the same `type` field the BUY/SELL branch parses.
    _REDEEM_TYPES = ("redeem", "claim", "reward")

    for item in activity:
        item_type = str(item.get("type", "")).lower()
        is_redeem = item_type in _REDEEM_TYPES
        if item_type not in ("trade", "buy", "sell") and not is_redeem:
            continue

        market_id = str(item.get("market", item.get("conditionId", "")))
        token_id = str(item.get("asset", item.get("tokenId", "")))
        side = str(item.get("side", "")).upper()

        try:
            price = float(item.get("price", 0))
            size = float(item.get("size", item.get("usdcSize", 0)))
        except (ValueError, TypeError):
            continue

        ts_raw = item.get("timestamp", item.get("createdAt", 0))
        ts = _parse_timestamp(ts_raw)
        last_trade_ts = max(last_trade_ts, ts)

        key = (market_id, token_id)

        if is_redeem:
            # A redemption closes any still-open FIFO buy(s) for this
            # (market, token) at the payout price. Winning Polymarket shares
            # redeem at $1.00. If the record carries an explicit per-share
            # price, prefer it; otherwise default winning redemptions to 1.0.
            payout_price = price if price > 0 else 1.0
            while key in open_buys and open_buys[key]:
                buy = open_buys[key].pop(0)  # FIFO: oldest open buy first
                # Use buy-side shares: position size was fixed at entry.
                buy_shares = buy["size"] / max(buy["price"], _EPSILON)
                pnl_dollars = (payout_price - buy["price"]) * buy_shares
                # Score by ROI, not absolute dollars.
                cost_basis = buy["price"] * buy_shares
                if cost_basis <= _EPSILON:
                    # Zero-cost basis makes ROI undefined; skip it.
                    continue
                roi = pnl_dollars / cost_basis
                trade_records.append(
                    TradeRecord(
                        trade_id=str(item.get("id", "")),
                        market_id=market_id,
                        pnl=roi,  # ROI fraction
                        is_win=pnl_dollars > 0,
                        executed_at=ts,
                    )
                )

        elif side == "BUY":
            if key not in open_buys:
                open_buys[key] = []
            open_buys[key].append({"price": price, "size": size, "ts": ts})

        elif side == "SELL" and key in open_buys and open_buys[key]:
            buy = open_buys[key].pop(0)  # FIFO matching
            # Use buy-side shares: the position size was fixed at entry, not at exit.
            buy_shares = buy["size"] / max(buy["price"], _EPSILON)
            pnl_dollars = (price - buy["price"]) * buy_shares
            # Score by return on capital, not absolute dollars.
            cost_basis = buy["price"] * buy_shares
            if cost_basis <= _EPSILON:
                # Zero-cost basis (price ~0) makes ROI undefined; skip it.
                continue
            roi = pnl_dollars / cost_basis
            trade_records.append(
                TradeRecord(
                    trade_id=str(item.get("id", "")),
                    market_id=market_id,
                    pnl=roi,  # ROI fraction, e.g. 0.10 = +10%
                    is_win=pnl_dollars > 0,
                    executed_at=ts,
                )
            )

    if not trade_records:
        return TraderStats(
            address=address,
            pseudonym=pseudonym,
            total_pnl=total_pnl,
            trade_count=len(activity),
            win_rate=0.0,
            pnl_per_trade=[],
            last_trade_time=last_trade_ts,
        )

    wins = sum(1 for t in trade_records if t.is_win)
    win_rate = wins / len(trade_records)
    pnl_per_trade = [t.pnl for t in trade_records]

    return TraderStats(
        address=address,
        pseudonym=pseudonym,
        total_pnl=total_pnl,
        trade_count=len(trade_records),
        win_rate=win_rate,
        pnl_per_trade=pnl_per_trade,
        last_trade_time=last_trade_ts,
    )


def _parse_timestamp(raw) -> float:
    """Parse ISO 8601 string or numeric timestamp to Unix float.

    Returns 0.0 on parse failure (L4). The old fallback of time.time()
    fabricated "just traded now" freshness for dormant or data-missing traders,
    inflating their recency score. 0.0 lets _recency_weight() return 0.0 for
    those traders instead, which is the correct signal: unknown = stale.
    """
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

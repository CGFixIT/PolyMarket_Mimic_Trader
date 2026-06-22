"""
core/risk_manager.py — Range-relative Take Profit / Stop Loss for Polymarket

WHY THIS EXISTS
---------------
Polymarket tokens are bounded in [0, 1] (0¢ to 100¢ per share). Standard
flat-percentage TP/SL is logically broken at range extremes:

    Entry $0.82 → 15% TP = $0.943  (captures only 67% of remaining upside)
    Entry $0.97 → 15% TP = $1.12   (IMPOSSIBLE — token max is $1.00)
    Entry $0.02 →  5% SL = $0.019  (noise-triggered; $0.001 move stops you out)

THE FIX: thresholds are expressed as fractions of the *remaining distance*
to the token's natural ceiling (1.0) or floor (0.0):

    dist_to_ceil  = 1.0 − entry_price   ← remaining upside
    dist_to_floor = entry_price          ← remaining downside

    TP price = entry + max(dist_to_ceil  × tp_fraction,  min_tp_abs)
    SL price = entry − max(dist_to_floor × sl_fraction,  min_sl_abs)

    Both results are clamped to [0.0, 1.0].

EXAMPLES (defaults: tp_fraction=0.40, sl_fraction=0.25)
--------------------------------------------------------
  Entry  │  TP     │  SL     │  Risk:Reward (remaining range)
  ────────┼─────────┼─────────┼──────────────────────────────────
  $0.20  │  $0.52  │  $0.15  │  4.4:1 (TP captures 40% of $0.80 upside)
  $0.50  │  $0.70  │  $0.375 │  2.2:1 (TP captures 40% of $0.50 upside)
  $0.82  │  $0.892 │  $0.615 │  1.7:1 (TP captures 40% of $0.18 upside)
  $0.97  │  $1.00* │  $0.727 │  *clamped to token ceiling
  $0.02  │  $0.412 │  $0.00* │  *floored to token floor
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_EPSILON = 1e-9


# ─── Enums ────────────────────────────────────────────────────────────────────

class ExitReason(Enum):
    HOLD              = auto()
    TAKE_PROFIT       = auto()
    STOP_LOSS         = auto()
    TRAILING_STOP     = auto()
    TIME_EXIT         = auto()
    DAILY_LOSS_LIMIT  = auto()  # Portfolio circuit breaker
    MARKET_RESOLVING  = auto()  # Within resolution blackout window
    EXPOSURE_CAP      = auto()  # Market-level cap would be breached
    SOURCE_EXIT       = auto()  # Tracked trader exited; we mirror their exit


class Side(Enum):
    BUY  = "BUY"
    SELL = "SELL"


# ─── Custom Exceptions ────────────────────────────────────────────────────────

class ExposureCapError(RuntimeError):
    """Raised when a new position would breach the per-market exposure cap."""


class InvalidPriceError(ValueError):
    """Raised when a price is outside the valid Polymarket token range [0, 1]."""


# ─── Configuration ────────────────────────────────────────────────────────────

@dataclass
class RiskConfig:
    # --- Range-relative threshold fractions ---
    tp_range_fraction: float        = 0.40   # Capture 40% of remaining upside
    sl_range_fraction: float        = 0.25   # Risk 25% of remaining downside

    # --- Absolute minimum distances (guard rail for extreme entries) ---
    min_tp_abs: float               = 0.03
    min_sl_abs: float               = 0.02

    # --- Trailing stop ---
    trailing_stop_fraction: float   = 0.15

    # --- Time-based exit ---
    time_exit_hours: float          = 48.0
    time_exit_min_range_move: float = 0.10   # < 10% of remaining range → stale, exit

    # --- Portfolio-level circuit breakers ---
    daily_loss_limit_pct: float     = 0.03   # Stop all trading if daily loss > 3% bankroll
    max_market_exposure_pct: float  = 0.08   # Max 8% of bankroll in any one market
    max_trader_allocation: float    = 0.05   # Max 5% of bankroll copied from any one trader

    # --- Post-loss cooldown ---
    cooldown_after_losses: int      = 3      # Pause entries after N consecutive losses
    cooldown_minutes: int           = 60     # Length of that pause

    # --- Market resolution blackout ---
    resolution_blackout_hours: float = 24.0


# ─── Position ─────────────────────────────────────────────────────────────────

@dataclass
class Position:
    """
    Represents a single open copy-trade position.
    Always construct via RiskManager.build_position() to guarantee
    range-relative TP/SL are computed and market exposure is registered.
    """
    position_id:    str
    market_id:      str
    token_id:       str
    trader_address: str              # Wallet we're copying
    side:           Side
    entry_price:    float
    size_shares:    float
    entry_time:     float            = field(default_factory=time.time)

    tp_price:       Optional[float]  = None
    sl_price:       Optional[float]  = None
    peak_price:     Optional[float]  = None  # Tracks highest price since entry
    resolve_time:   Optional[float]  = None  # Unix timestamp of market resolution

    def __post_init__(self):
        if self.tp_price is None:
            raise ValueError(
                "tp_price is None. Construct positions via RiskManager.build_position()."
            )
        if self.sl_price is None:
            raise ValueError(
                "sl_price is None. Construct positions via RiskManager.build_position()."
            )
        if self.peak_price is None:
            self.peak_price = self.entry_price

    def pnl_at(self, current_price: float) -> float:
        """Unrealized (or realized) PnL at a given price."""
        if self.side == Side.BUY:
            return (current_price - self.entry_price) * self.size_shares
        return (self.entry_price - current_price) * self.size_shares

    def __repr__(self) -> str:
        return (
            f"Position(id={self.position_id!r}, entry={self.entry_price:.4f}, "
            f"tp={self.tp_price:.4f}, sl={self.sl_price:.4f}, "
            f"peak={self.peak_price:.4f})"
        )


# ─── RiskManager ──────────────────────────────────────────────────────────────

class RiskManager:
    """
    Stateful risk engine for a copy-trading bot on Polymarket.

    Responsibilities:
    - Compute range-relative TP/SL for every new position
    - Evaluate open positions on every price tick
    - Track and enforce per-market exposure caps
    - Track and enforce daily loss circuit breaker
    - Record exits and update bankroll / daily PnL
    """

    def __init__(self, config: RiskConfig, bankroll: float):
        if bankroll <= 0:
            raise ValueError(f"Bankroll must be positive. Got: {bankroll}")
        self.cfg               = config
        self.bankroll          = bankroll
        self._daily_pnl        = 0.0
        self._day_start_ts     = _midnight_utc()
        # market_id → total $ value currently allocated in that market
        self._market_exposure: Dict[str, float] = {}
        # trader_address → total $ value currently copied from that trader
        self._trader_exposure: Dict[str, float] = {}
        self._exposure_lock = asyncio.Lock()
        # Post-loss cooldown state
        self._consecutive_losses: int = 0
        self._cooldown_until: float   = 0.0

    # ── Position factory ──────────────────────────────────────────────────────

    async def build_position(
        self,
        position_id:    str,
        market_id:      str,
        token_id:       str,
        trader_address: str,
        entry_price:    float,
        size_shares:    float,
        resolve_time:   Optional[float] = None,
    ) -> Position:
        """
        Validate price, compute range-relative TP/SL, check exposure cap,
        and return a fully initialised Position.

        Raises:
            InvalidPriceError   — if entry_price ∉ [0.0, 1.0]
            ExposureCapError    — if position would breach per-market cap
        """
        _assert_valid_price(entry_price, "entry_price")

        tp, sl = self._compute_thresholds(entry_price)
        if tp <= sl:
            raise InvalidPriceError(
                f"Degenerate thresholds at entry={entry_price}: tp={tp} <= sl={sl}"
            )

        position_value = entry_price * size_shares

        async with self._exposure_lock:
            self._assert_exposure_cap(market_id, position_value)
            self._assert_trader_allocation(trader_address, position_value)

            pos = Position(
                position_id    = position_id,
                market_id      = market_id,
                token_id       = token_id,
                trader_address = trader_address,
                side           = Side.BUY,
                entry_price    = entry_price,
                size_shares    = size_shares,
                tp_price       = tp,
                sl_price       = sl,
                peak_price     = entry_price,
                resolve_time   = resolve_time,
            )

            self._market_exposure[market_id] = (
                self._market_exposure.get(market_id, 0.0) + position_value
            )
            self._trader_exposure[trader_address] = (
                self._trader_exposure.get(trader_address, 0.0) + position_value
            )

        logger.info(
            "build_position | id=%-20s mkt=%s entry=%.4f TP=%.4f SL=%.4f "
            "upside_range=%.4f downside_range=%.4f size=%.1f",
            position_id, market_id, entry_price, tp, sl,
            1.0 - entry_price, entry_price, size_shares,
        )
        return pos

    # ── Per-tick evaluation ───────────────────────────────────────────────────

    def evaluate(self, pos: Position, current_price: float) -> ExitReason:
        """
        Check all exit conditions against current_price.
        Updates pos.peak_price in-place for trailing stop tracking.

        Priority order:
          0. Daily loss circuit breaker  (portfolio-level)
          1. Market resolution blackout  (market-level)
          2. Take profit                 (position-level)
          3. Hard stop loss              (position-level)
          4. Trailing stop               (position-level, only after a new high)
          5. Time exit                   (position-level, stale trade cleanup)
        """
        _assert_valid_price(current_price, "current_price")
        self._maybe_reset_daily_window()

        # 0 ── Daily loss circuit breaker ─────────────────────────────────────
        daily_loss_limit = -(self.bankroll * self.cfg.daily_loss_limit_pct)
        if self._daily_pnl <= daily_loss_limit:
            logger.warning(
                "DAILY LOSS LIMIT | daily_pnl=%.2f limit=%.2f bankroll=%.2f",
                self._daily_pnl, daily_loss_limit, self.bankroll,
            )
            return ExitReason.DAILY_LOSS_LIMIT

        # 1 ── Market resolution blackout ──────────────────────────────────────
        if pos.resolve_time is not None:
            hours_to_resolve = (pos.resolve_time - time.time()) / 3_600.0
            if 0.0 < hours_to_resolve < self.cfg.resolution_blackout_hours:
                logger.info(
                    "RESOLUTION BLACKOUT | id=%s mkt=%s resolves_in=%.1fh",
                    pos.position_id, pos.market_id, hours_to_resolve,
                )
                return ExitReason.MARKET_RESOLVING

        # 2 ── Compute effective peak (do NOT mutate pos — caller persists to DB) ──
        effective_peak = max(pos.peak_price, current_price)

        # 3 ── Take profit ──────────────────────────────────────────────────────
        if current_price >= pos.tp_price:
            logger.info(
                "TAKE PROFIT | id=%s price=%.4f tp=%.4f gain_pct=%.1f%%",
                pos.position_id, current_price, pos.tp_price,
                (current_price - pos.entry_price) / pos.entry_price * 100,
            )
            return ExitReason.TAKE_PROFIT

        # 4 ── Hard stop loss ──────────────────────────────────────────────────
        if current_price <= pos.sl_price:
            logger.info(
                "STOP LOSS | id=%s price=%.4f sl=%.4f loss_pct=%.1f%%",
                pos.position_id, current_price, pos.sl_price,
                (current_price - pos.entry_price) / pos.entry_price * 100,
            )
            return ExitReason.STOP_LOSS

        # 5 ── Trailing stop (only after price made a new high above entry) ────
        if effective_peak > pos.entry_price:
            trail_sl = self._compute_trail_sl(pos, peak_override=effective_peak)
            if current_price <= trail_sl:
                logger.info(
                    "TRAILING STOP | id=%s price=%.4f peak=%.4f trail_sl=%.4f",
                    pos.position_id, current_price, effective_peak, trail_sl,
                )
                return ExitReason.TRAILING_STOP

        # 6 ── Time exit: stale trade with minimal range movement ──────────────
        elapsed_hours = (time.time() - pos.entry_time) / 3_600.0
        if elapsed_hours >= self.cfg.time_exit_hours:
            working_range = max(pos.tp_price - pos.sl_price, _EPSILON)
            range_move    = abs(current_price - pos.entry_price) / working_range
            if range_move < self.cfg.time_exit_min_range_move:
                logger.info(
                    "TIME EXIT | id=%s elapsed=%.1fh range_move=%.3f threshold=%.3f",
                    pos.position_id, elapsed_hours, range_move,
                    self.cfg.time_exit_min_range_move,
                )
                return ExitReason.TIME_EXIT

        return ExitReason.HOLD

    # ── Record a closed position ───────────────────────────────────────────────

    async def record_exit(self, pos: Position, exit_price: float) -> float:
        """
        Call after a position closes (any ExitReason except HOLD).
        Updates bankroll, daily PnL, and releases market exposure.
        Returns realized PnL (negative = loss).
        """
        pnl = pos.pnl_at(exit_price)

        async with self._exposure_lock:
            self._daily_pnl  += pnl
            self.bankroll    += pnl

            released = pos.entry_price * pos.size_shares
            self._market_exposure[pos.market_id] = max(
                0.0,
                self._market_exposure.get(pos.market_id, 0.0) - released,
            )
            self._trader_exposure[pos.trader_address] = max(
                0.0,
                self._trader_exposure.get(pos.trader_address, 0.0) - released,
            )

        self._update_cooldown(pnl)

        logger.info(
            "record_exit | id=%s exit=%.4f pnl=%+.4f daily_pnl=%+.4f bankroll=%.2f",
            pos.position_id, exit_price, pnl, self._daily_pnl, self.bankroll,
        )
        return pnl

    def _update_cooldown(self, pnl: float) -> None:
        """Track consecutive losses and engage a cooldown after a losing streak.
        Any win resets the streak."""
        if pnl < 0:
            self._consecutive_losses += 1
            if (
                self.cfg.cooldown_after_losses > 0
                and self._consecutive_losses >= self.cfg.cooldown_after_losses
            ):
                self._cooldown_until = time.time() + self.cfg.cooldown_minutes * 60
                logger.warning(
                    "COOLDOWN engaged for %d min after %d consecutive losses",
                    self.cfg.cooldown_minutes, self._consecutive_losses,
                )
                self._consecutive_losses = 0
        else:
            self._consecutive_losses = 0

    def is_trading_halted(self) -> Optional[str]:
        """Return a reason string if NEW entries should be blocked, else None.

        Checked on the ENTRY path so the daily-loss circuit breaker and the
        post-loss cooldown cannot be bypassed by opening fresh positions — the
        per-tick evaluate() only governs EXITS of already-open positions.
        """
        self._maybe_reset_daily_window()

        daily_loss_limit = -(self.bankroll * self.cfg.daily_loss_limit_pct)
        if self._daily_pnl <= daily_loss_limit:
            return (
                f"daily loss limit (daily_pnl=${self._daily_pnl:.2f} "
                f"<= ${daily_loss_limit:.2f})"
            )

        remaining = self._cooldown_until - time.time()
        if remaining > 0:
            return (
                f"post-loss cooldown active for {remaining / 60.0:.1f} more min"
            )

        return None

    # ── Public helpers ────────────────────────────────────────────────────────

    def market_exposure(self, market_id: str) -> float:
        """Current $ allocated in a given market."""
        return self._market_exposure.get(market_id, 0.0)

    async def release_exposure(
        self, market_id: str, value: float, trader_address: Optional[str] = None
    ) -> None:
        """Release exposure registered by build_position() for a position that was
        never actually opened (e.g. order placement failed). Unlike record_exit,
        this does NOT touch bankroll or daily PnL — no trade occurred.

        Pass ``trader_address`` to also release the per-trader allocation that
        build_position() reserved; otherwise it would leak and slowly choke off
        future copies from that trader."""
        async with self._exposure_lock:
            self._market_exposure[market_id] = max(
                0.0, self._market_exposure.get(market_id, 0.0) - value
            )
            if trader_address is not None:
                self._trader_exposure[trader_address] = max(
                    0.0, self._trader_exposure.get(trader_address, 0.0) - value
                )

    def market_exposure_cap(self) -> float:
        """Current cap in $ terms (changes as bankroll changes)."""
        return self.bankroll * self.cfg.max_market_exposure_pct

    def trader_exposure(self, trader_address: str) -> float:
        """Current $ copied from a given trader across all open positions."""
        return self._trader_exposure.get(trader_address, 0.0)

    def trader_allocation_cap(self) -> float:
        """Per-trader allocation cap in $ terms (changes as bankroll changes)."""
        return self.bankroll * self.cfg.max_trader_allocation

    def daily_pnl(self) -> float:
        return self._daily_pnl

    # ── Internal threshold computation ────────────────────────────────────────

    def _compute_thresholds(self, entry: float) -> Tuple[float, float]:
        """
        Range-relative TP and SL with absolute minimum guards.

        TP = entry + max(dist_to_ceil × tp_fraction,  min_tp_abs), then clamped ≤ 1.0
        SL = entry − max(dist_to_floor × sl_fraction, min_sl_abs), then clamped ≥ 0.0

        Near-boundary entries get adaptive minimums so TP/SL remain meaningful:
          entry < 0.02 → min_sl at least 50% of entry (prevents SL clamping to 0)
          entry > 0.98 → min_tp at least 50% of remaining upside
        """
        dist_ceil  = 1.0 - entry   # remaining upside
        dist_floor = entry         # remaining downside

        # Adaptive minimums guard against degenerate TP/SL near price extremes.
        min_tp = self.cfg.min_tp_abs
        min_sl = self.cfg.min_sl_abs
        if entry < 0.02:
            min_sl = max(min_sl, entry * 0.5)
        if entry > 0.98:
            min_tp = max(min_tp, dist_ceil * 0.5)

        tp_raw = entry + max(dist_ceil  * self.cfg.tp_range_fraction, min_tp)
        sl_raw = entry - max(dist_floor * self.cfg.sl_range_fraction, min_sl)

        tp = min(tp_raw, 1.0)
        sl = max(sl_raw, 0.0)

        if tp <= sl:
            raise InvalidPriceError(
                f"Entry {entry:.4f} produces TP={tp:.4f} ≤ SL={sl:.4f}. "
                "Widen min_tp_abs / min_sl_abs in config or reject this entry."
            )

        return round(tp, 6), round(sl, 6)

    def _compute_trail_sl(self, pos: Position, peak_override: Optional[float] = None) -> float:
        """
        Trailing SL = peak − (peak − hard_SL) × trailing_fraction.
        Never drops below the hard SL.
        """
        peak     = peak_override if peak_override is not None else pos.peak_price
        gap      = peak - pos.sl_price
        trail_sl = peak - (gap * self.cfg.trailing_stop_fraction)
        return max(trail_sl, pos.sl_price)

    def _assert_exposure_cap(self, market_id: str, new_value: float) -> None:
        cap     = self.market_exposure_cap()
        current = self._market_exposure.get(market_id, 0.0)
        if current + new_value > cap:
            raise ExposureCapError(
                f"Market {market_id}: existing=${current:.2f} + new=${new_value:.2f} "
                f"= ${current + new_value:.2f} > cap=${cap:.2f} "
                f"({self.cfg.max_market_exposure_pct * 100:.0f}% of ${self.bankroll:.2f})"
            )

    def _assert_trader_allocation(self, trader_address: str, new_value: float) -> None:
        cap     = self.trader_allocation_cap()
        current = self._trader_exposure.get(trader_address, 0.0)
        if current + new_value > cap:
            raise ExposureCapError(
                f"Trader {trader_address[:10]}: existing=${current:.2f} + "
                f"new=${new_value:.2f} = ${current + new_value:.2f} > cap=${cap:.2f} "
                f"({self.cfg.max_trader_allocation * 100:.0f}% of ${self.bankroll:.2f})"
            )

    def _maybe_reset_daily_window(self) -> None:
        now_utc   = datetime.fromtimestamp(time.time(), tz=timezone.utc)
        start_utc = datetime.fromtimestamp(self._day_start_ts, tz=timezone.utc)
        if now_utc.date() != start_utc.date():
            logger.info("Daily PnL window reset. Previous daily_pnl=%.2f", self._daily_pnl)
            self._daily_pnl    = 0.0
            self._day_start_ts = _midnight_utc()


# ─── Module-level helpers ──────────────────────────────────────────────────────

def _assert_valid_price(price: float, name: str = "price") -> None:
    if not (0.0 <= price <= 1.0):
        raise InvalidPriceError(
            f"{name} must be in [0.0, 1.0] (Polymarket token range). Got: {price!r}"
        )


def _midnight_utc() -> float:
    """Unix timestamp of midnight UTC of the current day.

    time.mktime() interprets struct_time in LOCAL time, which produces the
    wrong reset point on non-UTC servers. Using timezone-aware datetime
    guarantees the daily-loss window always resets at 00:00:00 UTC regardless
    of the host's system timezone.
    """
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

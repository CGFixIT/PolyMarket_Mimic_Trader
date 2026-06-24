"""Configuration loading from .env and config.yaml."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


class ConfigError(ValueError):
    """Raised when configuration is invalid (bad bankroll, missing live key, …).

    Raising a typed exception instead of calling sys.exit() keeps load_config
    importable and unit-testable: callers can assert on the error, and the CLI
    entrypoint translates it into a clean exit (see main.main())."""


class TraderSelectionConfig(BaseModel):
    min_pnl: float = 10000
    min_win_rate: float = 0.55
    min_trades: int = 50
    rebalance_days: int = 7
    half_life_days: float = 14.0
    max_top_traders: int = 5
    # H14: cap Sharpe proxy at this value to prevent outliers (e.g., lucky streaks)
    sharpe_cap: float = 3.0
    # H14: shrink Sharpe toward zero for samples smaller than this
    sharpe_shrink_min_trades: int = 20
    # H16: minimum expected ROI per trade (expectancy = mean_pnl * log(n+1))
    min_expectancy: float = 0.01
    # H15: also fetch leaderboard in this window (days) to filter for consistency
    recent_window_days: int = 30


class CopyTradingConfig(BaseModel):
    size_multiplier: float = 0.5
    max_trade_pct: float = 0.02
    max_trader_allocation: float = 0.05
    max_price_deviation: float = 0.02
    # H6: only adverse price moves (we'd pay more than the whale) gate the copy.
    # A favorable move (whale bought at 0.40, price now 0.36) is never rejected —
    # it's a better entry with more upside, not a reason to skip.
    # max_favorable_deviation caps collapsed prices (likely adverse news): if price
    # dropped more than 15% below the whale's entry we skip (probable signal decay).
    max_favorable_deviation: float = 0.15
    # H7: entry-price band gate — skip tokens trading at extreme prices where
    # edge after fees vanishes (0.97+ YES has ~3¢ upside vs 97¢ downside).
    min_entry_price: float = 0.05
    max_entry_price: float = 0.95
    max_concurrent_positions: int = 10
    # M9: cap concurrent open positions on any single token. Two tracked traders
    # buying the same token would otherwise open unbounded copies on one outcome,
    # concentrating idiosyncratic risk. 0 disables the per-token cap.
    max_positions_per_token: int = 3
    # M1: re-fetch the price after acquiring the entry lock and skip if it moved
    # adversely beyond max_price_deviation since detection. The lock-wait window
    # (another concurrent entry, sizing, DB I/O) lets the price drift; without
    # this we'd race a stale edge. False disables the second fetch.
    revalidate_edge_before_order: bool = True
    min_market_volume: float = 5000
    # Skip trades older than this at detection time — by then the source's alpha
    # has decayed and we'd only be buying into their price impact (adverse
    # selection). 0 disables the gate.
    max_trade_age_seconds: float = 12.0
    # When True, if a tracked trader sells a token we hold a copy position in,
    # treat it as an exit signal and close our position (ExitReason.SOURCE_EXIT).
    mirror_source_exits: bool = True
    # M5: order-type selection, made explicit and tunable.
    #   ENTRY → FOK (Fill-Or-Kill): all-or-nothing immediate fill. We want the full
    #     intended copy size at the validated price or nothing — a partial entry at a
    #     drifting average is worse than skipping. Never rests as a GTC limit at the
    #     midpoint (which fills adversely or never).
    #   EXIT  → FAK (Fill-And-Kill / IOC): take whatever liquidity is on the bid NOW,
    #     cancel the remainder. In a fast down-move a resting GTC limit trails the book
    #     and never liquidates (unbounded loss); FAK guarantees we hit available depth.
    # Both are configurable for venues/strategies that prefer different semantics.
    # Typed as the CLOB's valid order types so an invalid value is rejected at load.
    entry_order_type: Literal["GTC", "FOK", "GTD", "FAK"] = "FOK"
    exit_order_type: Literal["GTC", "FOK", "GTD", "FAK"] = "FAK"
    # Paper-mode fill simulation: apply half-spread slippage + taker fee so
    # paper PnL reflects live execution costs rather than zero-cost fills.
    paper_fill_slippage_pct: float = 0.005   # ~0.5% half-spread
    paper_taker_fee_pct: float = 0.02         # Polymarket CLOB taker fee
    # Live-mode slippage cap: reject a BUY if no ask depth exists within this
    # fraction of the requested price. Prevents inadvertently paying far above
    # the quoted price when the order book is thin or the market moves fast.
    # Must match or exceed paper_fill_slippage_pct so live/paper parity holds.
    max_live_slippage_pct: float = 0.01       # 1% max walk above order price
    # H5: Expected round-trip cost (entry slip + taker fee + exit fee) used for
    # the pre-copy edge check and the TP revalidation after fill reconciliation.
    # Default matches paper mode (0.5% slip + 2% fee + 2% exit fee ≈ 4.5%).
    round_trip_fee_pct: float = 0.045
    # Edge-aware (fractional-Kelly) position sizing. OFF by default — opt-in, so
    # enabling it is the only thing that changes copy-size behaviour. When on,
    # sizing uses kelly_size_usdc() with the trader's observed win rate, but only
    # once that trader has >= kelly_min_trades closed trades; otherwise the flat
    # size_multiplier formula is used. The max_trade_pct cap always applies.
    kelly_enabled: bool = False
    kelly_fraction_multiplier: float = 0.25
    kelly_min_trades: int = 20
    # When True (default), Kelly sizing can use the tracker's observed win rate as
    # a prior while our own closed-trade sample is smaller than kelly_min_trades.
    # The tracker's win rate comes from the live leaderboard and is not shaped by
    # our TP/SL rules, so it is a less biased estimate than the portfolio win rate
    # during the early warm-up period. Disabled automatically when the bot's own
    # sample reaches kelly_min_trades (the portfolio rate then takes over).
    kelly_seed_from_tracker: bool = True


class RiskManagementConfig(BaseModel):
    tp_range_fraction: float = 0.40
    sl_range_fraction: float = 0.25
    min_tp_abs: float = 0.03
    min_sl_abs: float = 0.02
    # L5: at low entry prices the remaining upside range is huge (entry=0.10 →
    # 0.90 to ceiling), so a 40%-of-range TP targets an unrealistic +360% move
    # that rarely fills before mean-reversion. Below low_entry_threshold we taper
    # tp_range_fraction down to low_entry_tp_fraction to set a more realistic,
    # conservative profit target that actually gets hit.
    low_entry_threshold: float = 0.20
    low_entry_tp_fraction: float = 0.25
    trailing_stop_fraction: float = 0.40   # H1: loosened (was 0.15)
    time_exit_hours: float = 48.0
    time_exit_min_range_move: float = 0.10
    min_reward_risk: float = 1.0           # H2: floor R:R ratio (SL capped to TP dist / ratio)
    daily_loss_limit_pct: float = 0.03
    max_market_exposure_pct: float = 0.08
    resolution_blackout_hours: float = 24.0
    drawdown_stop_pct: float = 0.08
    cooldown_after_losses: int = 3
    cooldown_minutes: int = 60
    # Fail CLOSED (skip) when market metadata or current price can't be fetched,
    # rather than trading on missing/stale data.
    fail_closed_on_missing_data: bool = True
    # H4: Maximum fraction of bankroll deployed across all open positions at once.
    max_total_exposure_pct: float = 0.30
    # H10: WebSocket reconnect backoff cap and fast exit-poll interval.
    # Cap ensures WS reconnects are attempted at least every 30s (not 80s+).
    ws_max_backoff_seconds: float = 30.0
    # When WS is down, check exits this often instead of the normal poll_interval.
    exit_poll_fast_seconds: float = 2.0


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "trades.log"


class AppConfig(BaseModel):
    mode: str = "paper"
    polling_interval_seconds: int = 8
    # H17: bound (seconds) on poll-interval jitter and per-wallet phase offset.
    # Randomizes the poll cadence so an observer can't predict when we detect a
    # whale's trade and front-run our copy. 0 disables (fixed periodic polling).
    poll_jitter_seconds: float = 2.0
    max_tracked_traders: int = 5

    trader_selection: TraderSelectionConfig = Field(default_factory=TraderSelectionConfig)
    copy_trading: CopyTradingConfig = Field(default_factory=CopyTradingConfig)
    risk_management: RiskManagementConfig = Field(default_factory=RiskManagementConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    private_key: str = ""
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""
    bankroll: float = 500
    # H9: seconds before the watchdog fires a stall alert; 0 = auto (3× poll_interval)
    detection_stall_alert_seconds: float = 0.0


def load_config(
    config_path: Optional[str] = None,
    env_path: Optional[str] = None,
) -> AppConfig:
    if env_path:
        load_dotenv(env_path)
    else:
        load_dotenv()

    yaml_data: dict = {}
    if config_path is None:
        config_path = os.getenv("CONFIG_PATH", "config.yaml")
    config_file = Path(config_path)
    if config_file.exists():
        with open(config_file) as f:
            yaml_data = yaml.safe_load(f) or {}

    config = AppConfig(**yaml_data)

    config.private_key = os.getenv("POLY_PRIVATE_KEY", "")
    config.api_key = os.getenv("POLY_API_KEY", "")
    config.api_secret = os.getenv("POLY_API_SECRET", "")
    config.api_passphrase = os.getenv("POLY_API_PASSPHRASE", "")

    bankroll_str = os.getenv("BANKROLL", "")
    if bankroll_str:
        try:
            config.bankroll = float(bankroll_str)
        except ValueError:
            raise ConfigError(
                f"BANKROLL must be a number, got: {bankroll_str!r}"
            ) from None

    if config.bankroll <= 0:
        raise ConfigError("BANKROLL must be positive")

    if config.mode == "live" and not config.private_key:
        raise ConfigError("POLY_PRIVATE_KEY required for live mode")

    return config

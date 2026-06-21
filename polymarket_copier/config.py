"""Configuration loading from .env and config.yaml."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


class TraderSelectionConfig(BaseModel):
    min_pnl: float = 10000
    min_win_rate: float = 0.55
    min_trades: int = 50
    rebalance_days: int = 7
    half_life_days: float = 14.0
    max_top_traders: int = 5


class CopyTradingConfig(BaseModel):
    size_multiplier: float = 0.5
    max_trade_pct: float = 0.02
    max_trader_allocation: float = 0.05
    max_price_deviation: float = 0.02
    max_concurrent_positions: int = 10
    min_market_volume: float = 5000
    # Skip trades older than this at detection time — by then the source's alpha
    # has decayed and we'd only be buying into their price impact (adverse
    # selection). 0 disables the gate.
    max_trade_age_seconds: float = 12.0
    # When True, if a tracked trader sells a token we hold a copy position in,
    # treat it as an exit signal and close our position (ExitReason.SOURCE_EXIT).
    mirror_source_exits: bool = True
    # Paper-mode fill simulation: apply half-spread slippage + taker fee so
    # paper PnL reflects live execution costs rather than zero-cost fills.
    paper_fill_slippage_pct: float = 0.005   # ~0.5% half-spread
    paper_taker_fee_pct: float = 0.02         # Polymarket CLOB taker fee
    # Edge-aware (fractional-Kelly) position sizing. OFF by default — opt-in, so
    # enabling it is the only thing that changes copy-size behaviour. When on,
    # sizing uses kelly_size_usdc() with the trader's observed win rate, but only
    # once that trader has >= kelly_min_trades closed trades; otherwise the flat
    # size_multiplier formula is used. The max_trade_pct cap always applies.
    kelly_enabled: bool = False
    kelly_fraction_multiplier: float = 0.25
    kelly_min_trades: int = 20


class RiskManagementConfig(BaseModel):
    tp_range_fraction: float = 0.40
    sl_range_fraction: float = 0.25
    min_tp_abs: float = 0.03
    min_sl_abs: float = 0.02
    trailing_stop_fraction: float = 0.15
    time_exit_hours: float = 48.0
    time_exit_min_range_move: float = 0.10
    daily_loss_limit_pct: float = 0.03
    max_market_exposure_pct: float = 0.08
    resolution_blackout_hours: float = 24.0
    drawdown_stop_pct: float = 0.08
    cooldown_after_losses: int = 3
    cooldown_minutes: int = 60
    # Fail CLOSED (skip) when market metadata or current price can't be fetched,
    # rather than trading on missing/stale data.
    fail_closed_on_missing_data: bool = True


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "trades.log"


class AppConfig(BaseModel):
    mode: str = "paper"
    polling_interval_seconds: int = 8
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
            print(
                f"ERROR: BANKROLL must be a number, got: {bankroll_str!r}",
                file=sys.stderr,
            )
            sys.exit(1)

    if config.bankroll <= 0:
        print("ERROR: BANKROLL must be positive", file=sys.stderr)
        sys.exit(1)

    if config.mode == "live" and not config.private_key:
        print("ERROR: POLY_PRIVATE_KEY required for live mode", file=sys.stderr)
        sys.exit(1)

    return config

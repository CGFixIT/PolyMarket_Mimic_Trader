"""Configuration loading from .env and config.yaml."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


class TraderSelectionConfig(BaseModel):
    min_pnl: float = 10000
    min_win_rate: float = 0.60
    min_trades: int = 50
    rebalance_days: int = 7


class CopyTradingConfig(BaseModel):
    size_multiplier: float = 0.5
    max_trade_pct: float = 0.02
    max_trader_allocation: float = 0.05
    max_price_deviation: float = 0.02
    max_concurrent_positions: int = 10


class RiskManagementConfig(BaseModel):
    take_profit_pct: float = 0.15
    stop_loss_pct: float = 0.05
    trailing_stop_pct: float = 0.10
    time_exit_hours: int = 48
    time_exit_min_move: float = 0.03
    daily_loss_limit_pct: float = 0.03
    drawdown_stop_pct: float = 0.08
    cooldown_after_losses: int = 3
    cooldown_minutes: int = 60
    min_market_volume: float = 5000


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "trades.log"


class AppConfig(BaseModel):
    mode: str = "paper"
    polling_interval_seconds: int = 20
    max_tracked_traders: int = 5

    trader_selection: TraderSelectionConfig = Field(default_factory=TraderSelectionConfig)
    copy_trading: CopyTradingConfig = Field(default_factory=CopyTradingConfig)
    risk_management: RiskManagementConfig = Field(default_factory=RiskManagementConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    # Credentials from env
    private_key: str = ""
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""
    bankroll: float = 10000


def load_config(
    config_path: Optional[str] = None,
    env_path: Optional[str] = None,
) -> AppConfig:
    """Load configuration from config.yaml and .env file."""
    # Load .env
    if env_path:
        load_dotenv(env_path)
    else:
        load_dotenv()

    # Load YAML config
    yaml_data: dict = {}
    if config_path is None:
        config_path = os.getenv("CONFIG_PATH", "config.yaml")
    config_file = Path(config_path)
    if config_file.exists():
        with open(config_file) as f:
            yaml_data = yaml.safe_load(f) or {}

    config = AppConfig(**yaml_data)

    # Override with env vars
    config.private_key = os.getenv("POLY_PRIVATE_KEY", "")
    config.api_key = os.getenv("POLY_API_KEY", "")
    config.api_secret = os.getenv("POLY_API_SECRET", "")
    config.api_passphrase = os.getenv("POLY_API_PASSPHRASE", "")

    bankroll_str = os.getenv("BANKROLL", "")
    if bankroll_str:
        config.bankroll = float(bankroll_str)

    return config

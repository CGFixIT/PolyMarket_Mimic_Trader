"""Tests for configuration loading."""

from __future__ import annotations

import os
import tempfile

import pytest
import yaml

from polymarket_copier.config import AppConfig, load_config


class TestAppConfig:
    def test_defaults(self):
        config = AppConfig()
        assert config.mode == "paper"
        assert config.polling_interval_seconds == 20
        assert config.max_tracked_traders == 5
        assert config.bankroll == 10000
        assert config.risk_management.take_profit_pct == 0.15
        assert config.risk_management.stop_loss_pct == 0.05
        assert config.risk_management.trailing_stop_pct == 0.10
        assert config.copy_trading.size_multiplier == 0.5
        assert config.copy_trading.max_trade_pct == 0.02

    def test_custom_values(self):
        config = AppConfig(
            mode="live",
            bankroll=50000,
            risk_management={"take_profit_pct": 0.20, "stop_loss_pct": 0.08},
        )
        assert config.mode == "live"
        assert config.bankroll == 50000
        assert config.risk_management.take_profit_pct == 0.20
        assert config.risk_management.stop_loss_pct == 0.08
        # Other defaults preserved
        assert config.risk_management.trailing_stop_pct == 0.10

    def test_load_config_from_yaml(self, tmp_path):
        yaml_content = {
            "mode": "live",
            "polling_interval_seconds": 30,
            "max_tracked_traders": 3,
            "copy_trading": {"size_multiplier": 0.3},
        }
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(yaml_content))

        config = load_config(config_path=str(config_file))
        assert config.mode == "live"
        assert config.polling_interval_seconds == 30
        assert config.max_tracked_traders == 3
        assert config.copy_trading.size_multiplier == 0.3

    def test_load_config_env_override(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"mode": "paper"}))

        env_file = tmp_path / ".env"
        env_file.write_text("BANKROLL=25000\nPOLY_PRIVATE_KEY=0xdeadbeef\n")

        monkeypatch.setenv("BANKROLL", "25000")
        monkeypatch.setenv("POLY_PRIVATE_KEY", "0xdeadbeef")

        config = load_config(config_path=str(config_file))
        assert config.bankroll == 25000
        assert config.private_key == "0xdeadbeef"

    def test_load_config_missing_yaml(self, tmp_path):
        config = load_config(config_path=str(tmp_path / "nonexistent.yaml"))
        # Should use all defaults
        assert config.mode == "paper"
        assert config.bankroll == 10000

    def test_nested_config_validation(self):
        config = AppConfig()
        assert config.trader_selection.min_pnl == 10000
        assert config.trader_selection.min_win_rate == 0.60
        assert config.trader_selection.min_trades == 50
        assert config.risk_management.cooldown_after_losses == 3
        assert config.risk_management.cooldown_minutes == 60

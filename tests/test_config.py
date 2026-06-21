"""Tests for v2 configuration loading."""

from __future__ import annotations

import pytest
import yaml

from polymarket_copier.config import AppConfig, load_config


class TestAppConfig:
    def test_defaults(self):
        config = AppConfig()
        assert config.mode == "paper"
        assert config.polling_interval_seconds == 8
        assert config.max_tracked_traders == 5
        assert config.bankroll == 500
        # v2 range-relative risk defaults
        assert config.risk_management.tp_range_fraction == 0.40
        assert config.risk_management.sl_range_fraction == 0.25
        assert config.risk_management.min_tp_abs == 0.03
        assert config.risk_management.min_sl_abs == 0.02
        assert config.risk_management.max_market_exposure_pct == 0.08
        assert config.risk_management.resolution_blackout_hours == 24.0
        assert config.copy_trading.size_multiplier == 0.5

    def test_custom_values(self):
        config = AppConfig(
            mode="live",
            bankroll=50000,
            risk_management={"tp_range_fraction": 0.50, "sl_range_fraction": 0.20},
        )
        assert config.mode == "live"
        assert config.bankroll == 50000
        assert config.risk_management.tp_range_fraction == 0.50
        assert config.risk_management.sl_range_fraction == 0.20
        # Other defaults preserved
        assert config.risk_management.min_tp_abs == 0.03

    def test_load_config_from_yaml(self, tmp_path):
        yaml_content = {
            "mode": "paper",
            "polling_interval_seconds": 12,
            "max_tracked_traders": 3,
            "copy_trading": {"size_multiplier": 0.3},
        }
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(yaml_content))

        config = load_config(config_path=str(config_file))
        assert config.mode == "paper"
        assert config.polling_interval_seconds == 12
        assert config.max_tracked_traders == 3
        assert config.copy_trading.size_multiplier == 0.3

    def test_load_config_env_override(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"mode": "paper"}))

        monkeypatch.setenv("BANKROLL", "25000")
        monkeypatch.setenv("POLY_PRIVATE_KEY", "0xdeadbeef")

        config = load_config(config_path=str(config_file))
        assert config.bankroll == 25000
        assert config.private_key == "0xdeadbeef"

    def test_load_config_missing_yaml(self, tmp_path):
        config = load_config(config_path=str(tmp_path / "nonexistent.yaml"))
        assert config.mode == "paper"
        assert config.bankroll == 500

    def test_load_config_invalid_bankroll_exits(self, tmp_path, monkeypatch):
        # A non-numeric BANKROLL env value must fail fast with a clear exit,
        # not crash with an unhandled ValueError deep in load_config.
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"mode": "paper"}))
        monkeypatch.setenv("BANKROLL", "not-a-number")
        with pytest.raises(SystemExit):
            load_config(config_path=str(config_file))

    def test_load_config_negative_bankroll_exits(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"mode": "paper"}))
        monkeypatch.setenv("BANKROLL", "-100")
        with pytest.raises(SystemExit):
            load_config(config_path=str(config_file))

    def test_trader_selection_v2_fields(self):
        config = AppConfig()
        assert config.trader_selection.min_pnl == 10000
        assert config.trader_selection.min_win_rate == 0.55
        assert config.trader_selection.min_trades == 50
        assert config.trader_selection.half_life_days == 14.0
        assert config.trader_selection.max_top_traders == 5

"""Tests for wallet address normalization utility."""

from __future__ import annotations

from unittest.mock import MagicMock

from polymarket_copier.core.copier import CopyTrader
from polymarket_copier.utils.addresses import normalize_address


class TestNormalizeAddress:
    def test_lowercase_passthrough(self):
        assert normalize_address("0xabcdef") == "0xabcdef"

    def test_uppercase_lowercased(self):
        assert normalize_address("0xABCDEF") == "0xabcdef"

    def test_mixed_case_lowercased(self):
        assert normalize_address("0xAbCdEf123456") == "0xabcdef123456"

    def test_empty_string_unchanged(self):
        assert normalize_address("") == ""

    def test_already_normalized_unchanged(self):
        addr = "0x1234567890abcdef"
        assert normalize_address(addr) == addr

    def test_full_address_length(self):
        addr = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
        assert normalize_address(addr) == addr.lower()


class TestCopyTraderAddressNormalization:
    def _make_copier(self):
        risk = MagicMock()
        risk.bankroll = 1000.0
        portfolio = MagicMock()
        clob = MagicMock()
        gamma = MagicMock()
        config = MagicMock()
        config.copy_trading.kelly_enabled = False
        config.copy_trading.max_trade_pct = 0.02
        config.copy_trading.size_multiplier = 0.5
        return CopyTrader(risk, portfolio, clob, gamma, config)

    def test_update_tracker_win_rates_normalizes_keys(self):
        copier = self._make_copier()
        copier.update_tracker_win_rates({"0xABCD": 0.65, "0xEFGH": 0.70})
        assert "0xabcd" in copier._tracker_win_rates
        assert "0xefgh" in copier._tracker_win_rates
        assert "0xABCD" not in copier._tracker_win_rates

    def test_update_tracker_mean_pnl_normalizes_keys(self):
        copier = self._make_copier()
        copier.update_tracker_mean_pnl({"0xABCD": 0.12, "0xEFGH": 0.08})
        assert "0xabcd" in copier._tracker_mean_pnl
        assert "0xefgh" in copier._tracker_mean_pnl
        assert "0xABCD" not in copier._tracker_mean_pnl

    def test_mixed_case_lookup_succeeds_after_normalize(self):
        """Addresses stored via uppercase keys are found by lowercase lookup."""
        copier = self._make_copier()
        copier.update_tracker_win_rates({"0xAbCd1234": 0.65})
        # event.wallet_address comes from monitor (lowercased)
        assert copier._tracker_win_rates.get("0xabcd1234") == 0.65

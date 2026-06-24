"""Shared test fixtures for the Polymarket copy trading bot v2."""

from __future__ import annotations

import pytest


@pytest.fixture
def sample_leaderboard_entry() -> dict:
    return {
        "name": "0xaaa111",
        "pseudonym": "WhaleOne",
        "pnl": 50000,
        "tradesCount": 200,
    }


@pytest.fixture
def sample_activity() -> list[dict]:
    """A round-trip BUY then SELL on the same market/token (a winning trade)."""
    return [
        {
            "id": "t1",
            "type": "trade",
            "side": "BUY",
            "market": "mkt-a",
            "asset": "tok-a",
            "price": "0.50",
            "size": "100",
            "timestamp": 1_700_000_000,
        },
        {
            "id": "t2",
            "type": "trade",
            "side": "SELL",
            "market": "mkt-a",
            "asset": "tok-a",
            "price": "0.65",
            "size": "65",
            "timestamp": 1_700_001_000,
        },
    ]

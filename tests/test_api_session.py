"""Tests for shared API-client session handling (pooling + creation race).

These guard the lazy-session machinery in GammaClient/DataClient:
  * concurrent first-callers must share ONE session (no orphaned ClientSession
    leak when copier fires get_market + get_market_price via asyncio.gather);
  * a legitimate 0.0 midpoint must be returned, not skipped as falsy.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from polymarket_copier.api.data_client import DataClient
from polymarket_copier.api.gamma_client import GammaClient


class _FakeConnector:
    def __init__(self, *args, **kwargs):
        pass


class _FakeClientSession:
    """Counts how many sessions get constructed."""

    instances = 0

    def __init__(self, *args, **kwargs):
        type(self).instances += 1
        self.closed = False

    async def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_counter():
    _FakeClientSession.instances = 0
    yield


@pytest.mark.parametrize("client_factory", [GammaClient, DataClient])
@pytest.mark.asyncio
async def test_concurrent_get_session_creates_single_session(client_factory):
    client = client_factory()
    with patch("aiohttp.ClientSession", _FakeClientSession), patch(
        "aiohttp.TCPConnector", _FakeConnector
    ):
        sessions = await asyncio.gather(*[client._get_session() for _ in range(25)])

    # Exactly one session built, and every caller got that same object.
    assert _FakeClientSession.instances == 1
    assert len({id(s) for s in sessions}) == 1


@pytest.mark.asyncio
async def test_get_session_reuses_open_session():
    client = GammaClient()
    with patch("aiohttp.ClientSession", _FakeClientSession), patch(
        "aiohttp.TCPConnector", _FakeConnector
    ):
        first = await client._get_session()
        second = await client._get_session()
    assert first is second
    assert _FakeClientSession.instances == 1


class _FakeResp:
    def __init__(self, data):
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        return None

    async def json(self):
        return self._data


class _FakeSession:
    def __init__(self, data):
        self._data = data

    def get(self, url, params=None):
        return _FakeResp(self._data)


@pytest.mark.asyncio
async def test_get_market_price_accepts_zero_midpoint():
    """A genuine 0.0 midpoint is valid in [0, 1] and must not be dropped."""
    client = GammaClient()
    from unittest.mock import AsyncMock

    session = _FakeSession({"mid": 0.0})
    with patch.object(client, "_get_session", new_callable=AsyncMock, return_value=session):
        price = await client.get_market_price("token-1")
    assert price == 0.0


@pytest.mark.asyncio
async def test_get_market_price_falls_through_to_midpoint_key():
    client = GammaClient()
    from unittest.mock import AsyncMock

    session = _FakeSession({"midpoint": "0.33"})
    with patch.object(client, "_get_session", new_callable=AsyncMock, return_value=session):
        price = await client.get_market_price("token-1")
    assert price == pytest.approx(0.33)

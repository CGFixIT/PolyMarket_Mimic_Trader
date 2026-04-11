"""Tests for the Gamma API client."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from polymarket_copier.api.gamma_client import GammaClient


@pytest.fixture
def gamma_client():
    return GammaClient(base_url="https://gamma-api.polymarket.com")


class TestGammaClient:
    @pytest.mark.asyncio
    async def test_get_markets_list(self, gamma_client):
        mock_data = [{"id": "m1", "question": "Will X happen?"}]
        with patch.object(gamma_client, "_get", new_callable=AsyncMock, return_value=mock_data):
            result = await gamma_client.get_markets()
            assert len(result) == 1
            assert result[0]["id"] == "m1"

    @pytest.mark.asyncio
    async def test_get_markets_dict(self, gamma_client):
        mock_data = {"markets": [{"id": "m1"}]}
        with patch.object(gamma_client, "_get", new_callable=AsyncMock, return_value=mock_data):
            result = await gamma_client.get_markets()
            assert len(result) == 1

    @pytest.mark.asyncio
    async def test_get_market_by_id(self, gamma_client):
        mock_data = {"id": "m1", "question": "Will X happen?", "midpoint": 0.65}
        with patch.object(gamma_client, "_get", new_callable=AsyncMock, return_value=mock_data):
            result = await gamma_client.get_market("m1")
            assert result["id"] == "m1"

    @pytest.mark.asyncio
    async def test_get_market_empty(self, gamma_client):
        with patch.object(gamma_client, "_get", new_callable=AsyncMock, return_value="not_a_dict"):
            result = await gamma_client.get_market("m1")
            assert result == {}

    @pytest.mark.asyncio
    async def test_get_events(self, gamma_client):
        mock_data = [{"id": "e1", "title": "Event 1"}]
        with patch.object(gamma_client, "_get", new_callable=AsyncMock, return_value=mock_data):
            result = await gamma_client.get_events()
            assert len(result) == 1

    @pytest.mark.asyncio
    async def test_get_market_price_midpoint(self, gamma_client):
        mock_data = {"midpoint": 0.72}
        with patch.object(gamma_client, "_get", new_callable=AsyncMock, return_value=mock_data):
            price = await gamma_client.get_market_price("token-1")
            assert price == pytest.approx(0.72)

    @pytest.mark.asyncio
    async def test_get_market_price_last_trade(self, gamma_client):
        mock_data = {"lastTradePrice": 0.55}
        with patch.object(gamma_client, "_get", new_callable=AsyncMock, return_value=mock_data):
            price = await gamma_client.get_market_price("token-1")
            assert price == pytest.approx(0.55)

    @pytest.mark.asyncio
    async def test_get_market_price_error(self, gamma_client):
        with patch.object(gamma_client, "_get", new_callable=AsyncMock, side_effect=Exception("timeout")):
            price = await gamma_client.get_market_price("token-1")
            assert price is None

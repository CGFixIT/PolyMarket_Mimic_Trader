"""Tests for the Data API client."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from polymarket_copier.api.data_client import DataClient


@pytest.fixture
def data_client():
    return DataClient(base_url="https://data-api.polymarket.com")


class TestDataClient:
    @pytest.mark.asyncio
    async def test_get_leaderboard_list_response(self, data_client):
        mock_data = [
            {"address": "0xaaa", "pnl": 50000, "numTrades": 200},
            {"address": "0xbbb", "pnl": 30000, "numTrades": 150},
        ]
        with patch.object(data_client, "_get", new_callable=AsyncMock, return_value=mock_data):
            result = await data_client.get_leaderboard()
            assert len(result) == 2
            assert result[0]["address"] == "0xaaa"

    @pytest.mark.asyncio
    async def test_get_leaderboard_dict_response(self, data_client):
        mock_data = {
            "leaderboard": [
                {"address": "0xaaa", "pnl": 50000},
            ]
        }
        with patch.object(data_client, "_get", new_callable=AsyncMock, return_value=mock_data):
            result = await data_client.get_leaderboard()
            assert len(result) == 1

    @pytest.mark.asyncio
    async def test_get_leaderboard_params(self, data_client):
        with patch.object(data_client, "_get", new_callable=AsyncMock, return_value=[]) as mock_get:
            await data_client.get_leaderboard(period="30d", order_by="volume", limit=10)
            mock_get.assert_called_once_with(
                "/leaderboard",
                params={"period": "30d", "sortBy": "volume", "limit": 10, "offset": 0},
            )

    @pytest.mark.asyncio
    async def test_get_trades_with_trader_filter(self, data_client):
        mock_trades = [{"id": "t1", "side": "BUY", "size": 100}]
        with patch.object(data_client, "_get", new_callable=AsyncMock, return_value=mock_trades):
            result = await data_client.get_trades(trader="0xabc")
            assert len(result) == 1

    @pytest.mark.asyncio
    async def test_get_trades_dict_response(self, data_client):
        mock_data = {"trades": [{"id": "t1"}]}
        with patch.object(data_client, "_get", new_callable=AsyncMock, return_value=mock_data):
            result = await data_client.get_trades()
            assert len(result) == 1

    @pytest.mark.asyncio
    async def test_get_activity(self, data_client):
        mock_data = [{"type": "trade", "amount": 100}]
        with patch.object(data_client, "_get", new_callable=AsyncMock, return_value=mock_data):
            result = await data_client.get_activity("0xabc")
            assert len(result) == 1

    @pytest.mark.asyncio
    async def test_close_session(self):
        mock_session = AsyncMock()
        client = DataClient(session=mock_session)
        # External session should not be closed
        await client.close()
        mock_session.close.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_own_session(self, data_client):
        mock_session = AsyncMock()
        mock_session.closed = False
        data_client._session = mock_session
        data_client._external_session = False
        await data_client.close()
        mock_session.close.assert_called_once()

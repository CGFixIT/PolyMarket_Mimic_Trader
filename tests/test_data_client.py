"""Tests for the v2 Data API client."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from polymarket_copier.api.data_client import DataClient


@pytest.fixture
def data_client():
    return DataClient(base_url="https://data-api.polymarket.com")


class TestDataClient:
    @pytest.mark.asyncio
    async def test_get_leaderboard_list_response(self, data_client):
        mock_data = [
            {"name": "0xaaa", "pnl": 50000},
            {"name": "0xbbb", "pnl": 30000},
        ]
        with patch.object(data_client, "_get", new_callable=AsyncMock, return_value=mock_data):
            result = await data_client.get_leaderboard()
            assert len(result) == 2
            assert result[0]["name"] == "0xaaa"

    @pytest.mark.asyncio
    async def test_get_leaderboard_dict_response(self, data_client):
        mock_data = {"leaderboard": [{"name": "0xaaa", "pnl": 50000}]}
        with patch.object(data_client, "_get", new_callable=AsyncMock, return_value=mock_data):
            result = await data_client.get_leaderboard()
            assert len(result) == 1

    @pytest.mark.asyncio
    async def test_get_leaderboard_params(self, data_client):
        with patch.object(data_client, "_get", new_callable=AsyncMock, return_value=[]) as mock_get:
            await data_client.get_leaderboard(limit=10)
            mock_get.assert_called_once_with(
                "/leaderboard", params={"window": "all", "limit": 10}
            )

    @pytest.mark.asyncio
    async def test_get_wallet_activity(self, data_client):
        mock_activity = [{"id": "t1", "side": "BUY", "size": 100}]
        with patch.object(data_client, "_get", new_callable=AsyncMock, return_value=mock_activity):
            result = await data_client.get_wallet_activity("0xabc")
            assert len(result) == 1

    @pytest.mark.asyncio
    async def test_get_wallet_activity_params(self, data_client):
        with patch.object(data_client, "_get", new_callable=AsyncMock, return_value=[]) as mock_get:
            await data_client.get_wallet_activity("0xabc", limit=50)
            mock_get.assert_called_once_with(
                "/activity", params={"user": "0xabc", "limit": 50}
            )

    @pytest.mark.asyncio
    async def test_close_external_session_not_closed(self):
        mock_session = AsyncMock()
        client = DataClient(session=mock_session)
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

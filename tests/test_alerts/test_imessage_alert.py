"""Tests for iMessage alert backend."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.alerts.imessage_alert import IMessageBackend


class TestIMessageBackend:
    @pytest.mark.asyncio
    async def test_send_success(self):
        backend = IMessageBackend("http://localhost:9999/alert")

        with patch("src.alerts.imessage_alert.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response = AsyncMock()
            mock_response.raise_for_status = lambda: None
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await backend.send("Test Title", "Test Body")

        assert result is True
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert call_args[1]["json"]["message"] == "Test Title\n\nTest Body"

    @pytest.mark.asyncio
    async def test_send_failure_returns_false(self):
        backend = IMessageBackend("http://localhost:9999/alert")

        with patch("src.alerts.imessage_alert.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=Exception("Connection refused"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await backend.send("Test", "Body")

        assert result is False

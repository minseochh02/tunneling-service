"""Tests for mobile enroll gateway proxy routes."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

from main import app  # noqa: E402


@pytest.fixture
def client():
    return TestClient(app)


def test_mobile_enroll_start_proxies_to_supabase(client):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = b'{"enroll_id":"x"}'
    mock_response.headers = {"content-type": "application/json"}

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("mobile_enroll_router.httpx.AsyncClient", return_value=mock_client):
        res = client.post(
            "/mobile/enroll/start",
            json={"device_nonce": "a" * 32, "phone_e164": "+821012345678"},
        )

    assert res.status_code == 200
    assert res.json()["enroll_id"] == "x"
    mock_client.post.assert_called_once()
    call_url = mock_client.post.call_args[0][0]
    assert call_url == "https://example.supabase.co/functions/v1/mobile-enroll-start"
    call_headers = mock_client.post.call_args.kwargs["headers"]
    assert call_headers["Authorization"] == "Bearer test-service-role-key"


def test_mobile_enroll_options(client):
    res = client.options("/mobile/enroll/status")
    assert res.status_code == 204
    assert res.headers.get("access-control-allow-origin") == "*"

"""Tests for offline tunnel HTML/JSON negotiation."""

import unittest
from unittest.mock import MagicMock

from tunnel_offline_response import (
    byo_tunnel_offline_response,
    tunnel_offline_html,
    tunnel_offline_json,
    tunnel_offline_response,
    wants_html_response,
)


def _request(method: str = "GET", accept: str | None = None) -> MagicMock:
    req = MagicMock()
    req.method = method
    headers = MagicMock()
    headers.get = lambda k, d=None: (
        accept if k.lower() == "accept" and accept is not None else d
    )
    req.headers = headers
    return req


class TunnelOfflineResponseTests(unittest.TestCase):
    def test_wants_html_for_browser_accept(self):
        self.assertTrue(wants_html_response(_request(accept="text/html,application/xhtml+xml")))

    def test_json_for_api_accept(self):
        self.assertFalse(wants_html_response(_request(accept="application/json")))

    def test_html_response_for_browser(self):
        req = _request(accept="text/html")
        resp = tunnel_offline_response(req, host="app.example.com", tunnel_id="my-tunnel")
        self.assertEqual(resp.status_code, 503)
        self.assertIn(b"Server not available", resp.body)
        self.assertIn(b"app.example.com", resp.body)

    def test_json_response_for_fetch(self):
        req = _request(accept="*/*")
        resp = tunnel_offline_response(req, tunnel_id="slug")
        self.assertEqual(resp.status_code, 503)
        body = resp.body.decode()
        self.assertIn("tunnel_offline", body)
        self.assertIn("slug", body)

    def test_byo_kind_in_html(self):
        html = tunnel_offline_html(kind="signin")
        self.assertIn("Sign-in unavailable", html)

    def test_byo_with_request_uses_signin_kind(self):
        req = _request(accept="text/html")
        resp = byo_tunnel_offline_response(req)
        self.assertIn(b"Sign-in unavailable", resp.body)

    def test_json_payload_fields(self):
        data = tunnel_offline_json(host="x.com", tunnel_id="t1")
        self.assertFalse(data["online"])
        self.assertTrue(data["retryable"])
        self.assertEqual(data["host"], "x.com")


if __name__ == "__main__":
    unittest.main()

"""Unit tests for visitor auth helpers."""

import unittest

from visitor_auth_router import (
    pending_id_from_callback_url,
    resolve_visitor_google_scopes,
    resolve_visitor_oauth_redirect_to,
    visitor_audience_from_return_to,
)


class VisitorAuthRouterTests(unittest.TestCase):
    def test_audience_from_return_to(self):
        self.assertEqual(
            visitor_audience_from_return_to("https://sheetbot.cloud/auth/callback"),
            "https://sheetbot.cloud",
        )

    def test_redirect_to_tunnel_callback(self):
        redirect = resolve_visitor_oauth_redirect_to(
            "abc123",
            "https://sheetbot.cloud/auth/callback",
            "https://tunneling-service.onrender.com/t/my-tunnel",
        )
        self.assertEqual(
            redirect,
            "https://tunneling-service.onrender.com/t/my-tunnel/visitor-auth/callback/abc123",
        )

    def test_redirect_to_localhost_allowlist(self):
        redirect = resolve_visitor_oauth_redirect_to(
            "abc123",
            "http://localhost:4001/auth/callback",
            "https://tunneling-service.onrender.com/t/my-tunnel",
        )
        self.assertEqual(redirect, "http://localhost:54321/auth/callback")

    def test_custom_domain_strips_tunnel_path(self):
        redirect = resolve_visitor_oauth_redirect_to(
            "abc123",
            "https://sheetbot.cloud/auth/callback",
            "https://sheetbot.cloud/t/mcp-server-fxkud1",
        )
        self.assertEqual(
            redirect,
            "https://sheetbot.cloud/visitor-auth/callback/abc123",
        )

    def test_pending_id_from_path(self):
        self.assertEqual(
            pending_id_from_callback_url(
                "https://tunneling-service.onrender.com/t/t1/visitor-auth/callback/pending-1"
            ),
            "pending-1",
        )
        self.assertEqual(
            pending_id_from_callback_url(
                "https://tunneling-service.onrender.com/t/t1/visitor-auth/callback/pending-1/complete"
            ),
            "pending-1",
        )

    def test_scopes_default_workspace(self):
        scopes = resolve_visitor_google_scopes(None)
        self.assertIn("https://www.googleapis.com/auth/spreadsheets", scopes)
        self.assertIn("openid", scopes)

    def test_scopes_custom(self):
        scopes = resolve_visitor_google_scopes(["https://www.googleapis.com/auth/drive.readonly"])
        self.assertIn("https://www.googleapis.com/auth/drive.readonly", scopes)
        self.assertIn("https://www.googleapis.com/auth/userinfo.email", scopes)


if __name__ == "__main__":
    unittest.main()

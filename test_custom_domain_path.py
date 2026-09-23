"""Unit tests for custom-domain path injection helpers."""

import unittest

from custom_domain_path import (
    inject_custom_domain_project_path,
    strip_tunnel_path_prefix,
    visitor_gateway_path,
)


class CustomDomainPathTests(unittest.TestCase):
    def test_inject_bare_path(self):
        self.assertEqual(inject_custom_domain_project_path("dashboard", "myapp"), "p/myapp/dashboard")
        self.assertEqual(inject_custom_domain_project_path("", "myapp"), "p/myapp")
        self.assertEqual(inject_custom_domain_project_path("/", "myapp"), "p/myapp")

    def test_no_double_prefix_same_project(self):
        self.assertEqual(
            inject_custom_domain_project_path("p/myapp/dashboard", "myapp"),
            "p/myapp/dashboard",
        )
        self.assertEqual(inject_custom_domain_project_path("p/myapp", "myapp"), "p/myapp")

    def test_replace_other_project_prefix(self):
        self.assertEqual(
            inject_custom_domain_project_path("p/other/dashboard", "myapp"),
            "p/myapp/dashboard",
        )

    def test_strip_tunnel_prefix_then_inject(self):
        self.assertEqual(
            inject_custom_domain_project_path("t/tunnel-1/p/oldapp/page", "myapp", "tunnel-1"),
            "p/myapp/page",
        )

    def test_strip_tunnel_prefix_only(self):
        self.assertEqual(strip_tunnel_path_prefix("t/abc/p/foo/bar", "abc"), "p/foo/bar")
        self.assertEqual(strip_tunnel_path_prefix("t/abc/assets/x.js", "abc"), "assets/x.js")

    def test_visitor_callback_strips_tunnel_prefix_on_custom_domain(self):
        self.assertEqual(
            visitor_gateway_path(
                "t/mcp-server-fxkud1/visitor-auth/callback/abc",
                "mcp-server-fxkud1",
            ),
            "visitor-auth/callback/abc",
        )
        self.assertEqual(
            visitor_gateway_path("visitor-auth/callback/abc", "mcp-server-fxkud1"),
            "visitor-auth/callback/abc",
        )
        self.assertEqual(
            visitor_gateway_path(
                "t/mcp-server-fxkud1/p/SheetBot/visitor-auth/callback/abc",
                "mcp-server-fxkud1",
            ),
            "visitor-auth/callback/abc",
        )
        self.assertIsNone(visitor_gateway_path("t/mcp-server-fxkud1/p/SheetBot/login", "mcp-server-fxkud1"))


if __name__ == "__main__":
    unittest.main()

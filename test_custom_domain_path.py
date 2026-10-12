"""Unit tests for custom-domain path injection helpers."""

import unittest

from custom_domain_path import (
    inject_canonical_visitor_origin_headers,
    inject_custom_domain_project_path,
    preferred_canonical_origin_for_project,
    preferred_canonical_origin_from_domain_map,
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

    def test_preferred_canonical_origin_prefers_apex(self):
        origin = preferred_canonical_origin_from_domain_map(
            {"www.shop.example": "App", "shop.example": "App"},
        )
        self.assertEqual(origin, "https://shop.example")

    def test_preferred_canonical_origin_for_project_scoped(self):
        domain_map = {
            "solgemtoken.com": "egdesk-system-demo",
            "www.solgemtoken.com": "egdesk-system-demo",
            "sheetbot.cloud": "SheetBot",
        }
        self.assertEqual(
            preferred_canonical_origin_for_project(domain_map, "egdesk-system-demo"),
            "https://solgemtoken.com",
        )
        self.assertEqual(
            preferred_canonical_origin_for_project(domain_map, "Sheetbot"),
            "https://sheetbot.cloud",
        )
        self.assertIsNone(preferred_canonical_origin_for_project(domain_map, "unmapped-project"))

    def test_vicky_cha4_sheetbot_tunnel_gets_no_canonical_headers(self):
        """Regression: tunnel-wide canonical pick used to send Sheetbot → solgemtoken.com."""
        domain_map = {
            "solgemtoken.com": "egdesk-system-demo",
            "www.solgemtoken.com": "egdesk-system-demo",
        }
        self.assertIsNone(preferred_canonical_origin_for_project(domain_map, "Sheetbot"))
        headers = inject_canonical_visitor_origin_headers(
            path="p/Sheetbot",
            request_host="tunneling-service.onrender.com",
            domain_project_map=domain_map,
        )
        self.assertEqual(headers, {})

    def test_egdesk_system_demo_tunnel_url_gets_no_redirect_hints(self):
        domain_map = {
            "solgemtoken.com": "egdesk-system-demo",
            "www.solgemtoken.com": "egdesk-system-demo",
        }
        self.assertEqual(
            preferred_canonical_origin_for_project(domain_map, "egdesk-system-demo"),
            "https://solgemtoken.com",
        )
        headers = inject_canonical_visitor_origin_headers(
            path="p/egdesk-system-demo/",
            request_host="tunneling-service.onrender.com",
            domain_project_map=domain_map,
        )
        self.assertEqual(headers, {})

    def test_inject_canonical_headers_for_www_alias_only(self):
        domain_map = {
            "solgemtoken.com": "egdesk-system-demo",
            "www.solgemtoken.com": "egdesk-system-demo",
        }
        headers = inject_canonical_visitor_origin_headers(
            path="dashboard",
            request_host="www.solgemtoken.com",
            domain_project_map=domain_map,
            project_override="egdesk-system-demo",
        )
        self.assertEqual(
            headers,
            {
                "x-egdesk-canonical-visitor-origin": "https://solgemtoken.com",
                "x-egdesk-canonical-redirect-enabled": "1",
            },
        )
        self.assertEqual(
            inject_canonical_visitor_origin_headers(
                path="",
                request_host="solgemtoken.com",
                domain_project_map=domain_map,
                project_override="egdesk-system-demo",
            ),
            {},
        )


if __name__ == "__main__":
    unittest.main()

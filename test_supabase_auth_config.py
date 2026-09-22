"""Unit tests for Supabase Auth redirect allow-list helpers."""

import unittest

from supabase_auth_config import (
    format_allow_list,
    host_needing_allowlist,
    merge_allow_list,
    parse_allow_list,
    redirect_pattern_for_host,
    remove_allow_patterns,
)


class SupabaseAuthAllowListTests(unittest.TestCase):
    def test_parse_comma_string_and_list(self):
        self.assertEqual(
            parse_allow_list("https://egdesk.cloud/**, http://localhost:54321/auth/callback"),
            ["https://egdesk.cloud/**", "http://localhost:54321/auth/callback"],
        )
        self.assertEqual(parse_allow_list(["https://a.com/**", ""]), ["https://a.com/**"])

    def test_merge_adds_custom_domain_without_dropping_existing(self):
        merged, changed = merge_allow_list(
            "https://egdesk.cloud/**,http://localhost:54321/auth/callback",
            [redirect_pattern_for_host("sheetbot.cloud")],
        )
        self.assertTrue(changed)
        self.assertIn("https://egdesk.cloud/**", merged)
        self.assertIn("https://sheetbot.cloud/**", merged)
        self.assertIn("http://localhost:54321/auth/callback", merged)

    def test_merge_is_idempotent(self):
        current = "https://sheetbot.cloud/**,https://egdesk.cloud/**"
        merged, changed = merge_allow_list(current, ["https://sheetbot.cloud/**"])
        self.assertFalse(changed)
        self.assertEqual(merged, current)

    def test_remove_only_the_custom_domain_pattern(self):
        updated, changed = remove_allow_patterns(
            "https://egdesk.cloud/**,https://sheetbot.cloud/**",
            ["https://sheetbot.cloud/**"],
        )
        self.assertTrue(changed)
        self.assertEqual(updated, "https://egdesk.cloud/**")

    def test_platform_hosts_do_not_need_a_per_login_update(self):
        self.assertIsNone(host_needing_allowlist("https://egdesk.cloud/auth/callback"))
        self.assertIsNone(
            host_needing_allowlist(
                "https://tunneling-service.onrender.com/t/demo/visitor-auth/callback/abc"
            )
        )
        self.assertIsNone(host_needing_allowlist("http://localhost:54321/auth/callback"))
        self.assertEqual(
            host_needing_allowlist("https://sheetbot.cloud/visitor-auth/callback/abc"),
            "sheetbot.cloud",
        )

    def test_format_dedupes(self):
        self.assertEqual(
            format_allow_list(["https://a.com/**", "https://a.com/**", " https://b.com/** "]),
            "https://a.com/**,https://b.com/**",
        )


if __name__ == "__main__":
    unittest.main()

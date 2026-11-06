"""Unit tests for gateway security fixes."""

import os
import tempfile
import unittest
import xml.etree.ElementTree as ET

from fedex_labels import _sanitize_label_id, load_label_pdf, save_label_pdf
from gateway_security import (
    constant_time_equals,
    extract_trusted_client_ip,
    is_valid_tunnel_identifier,
    lookup_mcp_server_by_key_or_name,
    safe_xml_fromstring,
    sanitize_tunnel_identifier,
    validate_google_drive_id,
    validate_outbound_webhook_url,
    verify_gateway_request_auth,
)
from main import _verify_row_owner_device


class _FakeQuery:
    def __init__(self):
        self.or_arg = None

    def select(self, *_args):
        return self

    def or_(self, expr: str):
        self.or_arg = expr
        return self

    def execute(self):
        class _Res:
            data = [{"server_key": "safe-key"}]

        return _Res()


class _FakeSupabase:
    def __init__(self):
        self.last_query = _FakeQuery()

    def table(self, _name: str):
        return self.last_query


class _FakeRequest:
    def __init__(self, headers: dict[str, str] | None = None):
        self.headers = headers or {}


class GatewaySecurityTests(unittest.TestCase):
    def test_tunnel_identifier_blocks_postgrest_injection(self):
        self.assertTrue(is_valid_tunnel_identifier("mcp-server-fxkud1"))
        self.assertTrue(is_valid_tunnel_identifier("vicky_cha4.prod"))
        self.assertFalse(is_valid_tunnel_identifier("foo,id.neq.0"))
        self.assertFalse(is_valid_tunnel_identifier("foo),name.eq.bar"))
        self.assertFalse(is_valid_tunnel_identifier("../etc/passwd"))
        self.assertIsNone(sanitize_tunnel_identifier("a,b"))

    def test_lookup_mcp_server_skips_malicious_identifier(self):
        fake_sb = _FakeSupabase()
        rows = lookup_mcp_server_by_key_or_name(fake_sb, "foo,id.neq.123")
        self.assertEqual(rows, [])
        self.assertIsNone(fake_sb.last_query.or_arg)

        rows_ok = lookup_mcp_server_by_key_or_name(fake_sb, "safe-key")
        self.assertEqual(len(rows_ok), 1)
        self.assertEqual(fake_sb.last_query.or_arg, "server_key.eq.safe-key,name.eq.safe-key")

    def test_safe_xml_blocks_xxe_and_billion_laughs(self):
        xxe_payload = """<?xml version="1.0"?>
        <!DOCTYPE lolz [
          <!ENTITY lol "lol">
        ]>
        <root>&lol;</root>"""
        with self.assertRaises(ET.ParseError):
            safe_xml_fromstring(xxe_payload)

        elem = safe_xml_fromstring("<response><tCnt>1</tCnt></response>")
        self.assertEqual(elem.findtext("tCnt"), "1")

    def test_fedex_label_blocks_path_traversal(self):
        self.assertIsNone(_sanitize_label_id("../../etc/passwd"))
        self.assertIsNone(_sanitize_label_id("../secret"))
        self.assertIsNone(_sanitize_label_id("a/b"))
        self.assertEqual(_sanitize_label_id("550e8400-e29b-41d4-a716-446655440000"), "550e8400-e29b-41d4-a716-446655440000")

        with tempfile.TemporaryDirectory() as tmpdir:
            old_dir = os.environ.get("FEDEX_LABEL_DIR")
            os.environ["FEDEX_LABEL_DIR"] = tmpdir
            try:
                with self.assertRaises(ValueError):
                    save_label_pdf("../escape", "default", b"%PDF-1.4")
                self.assertIsNone(load_label_pdf("../escape", "default"))
                saved = save_label_pdf("valid-uuid-1234", "tenant-1", b"%PDF-1.4 test")
                self.assertTrue(saved.is_file())
                self.assertEqual(load_label_pdf("valid-uuid-1234", "tenant-1"), b"%PDF-1.4 test")
            finally:
                if old_dir is None:
                    os.environ.pop("FEDEX_LABEL_DIR", None)
                else:
                    os.environ["FEDEX_LABEL_DIR"] = old_dir

    def test_ssrf_webhook_validation(self):
        with self.assertRaises(ValueError):
            validate_outbound_webhook_url("http://169.254.169.254/latest/meta-data/", allow_local=True, resolve_dns=False)
        with self.assertRaises(ValueError):
            validate_outbound_webhook_url("http://metadata.google.internal/computeMetadata/v1/", allow_local=True, resolve_dns=False)
        with self.assertRaises(ValueError):
            validate_outbound_webhook_url("file:///etc/passwd", allow_local=True, resolve_dns=False)
        with self.assertRaises(ValueError):
            validate_outbound_webhook_url("http://127.0.0.1:8080/hook", allow_local=False, resolve_dns=False)
        with self.assertRaises(ValueError):
            validate_outbound_webhook_url("http://10.0.0.1:8080/hook", allow_local=False, resolve_dns=False)

        ok_url = validate_outbound_webhook_url("https://93.184.216.34/webhook", allow_local=False, resolve_dns=False)
        self.assertEqual(ok_url, "https://93.184.216.34/webhook")

    def test_google_drive_id_validation_blocks_query_injection(self):
        self.assertEqual(validate_google_drive_id("1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms"), "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms")
        with self.assertRaises(ValueError):
            validate_google_drive_id("123' or name != '")

    def test_trusted_client_ip_ignores_spoofed_x_real_ip_and_leftmost_xff(self):
        headers = {
            "x-real-ip": "1.2.3.4",
            "x-forwarded-for": "1.2.3.4, 198.51.100.77, 10.10.1.5",
        }
        # Rightmost public hop is 198.51.100.77, not the attacker-controlled 1.2.3.4
        self.assertEqual(extract_trusted_client_ip(headers, "10.10.1.5"), "198.51.100.77")

    def test_device_binding_cannot_be_bypassed_by_omitting_device_id(self):
        server_data = {"owner_device_id": "device-abc"}
        ok_missing, _ = _verify_row_owner_device(server_data, None)
        self.assertFalse(ok_missing)
        ok_wrong, _ = _verify_row_owner_device(server_data, "device-other")
        self.assertFalse(ok_wrong)
        ok_match, _ = _verify_row_owner_device(server_data, "device-abc")
        self.assertTrue(ok_match)

    def test_gateway_auth_helper(self):
        os.environ["TEST_GATEWAY_KEY"] = "super-secret-key"
        try:
            ok_unauth, _ = verify_gateway_request_auth(
                _FakeRequest({}),
                env_keys=("TEST_GATEWAY_KEY",),
            )
            self.assertFalse(ok_unauth)

            ok_wrong, _ = verify_gateway_request_auth(
                _FakeRequest({"X-Api-Key": "wrong"}),
                env_keys=("TEST_GATEWAY_KEY",),
            )
            self.assertFalse(ok_wrong)

            ok_valid, _ = verify_gateway_request_auth(
                _FakeRequest({"X-Api-Key": "super-secret-key"}),
                env_keys=("TEST_GATEWAY_KEY",),
            )
            self.assertTrue(ok_valid)
        finally:
            os.environ.pop("TEST_GATEWAY_KEY", None)

    def test_constant_time_equals(self):
        self.assertTrue(constant_time_equals("abc", "abc"))
        self.assertFalse(constant_time_equals("abc", "def"))
        self.assertFalse(constant_time_equals("abc", None))


if __name__ == "__main__":
    unittest.main()

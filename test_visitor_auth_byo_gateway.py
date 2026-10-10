"""Routing tests for BYO vs platform visitor OAuth callbacks on the gateway."""

import unittest
from unittest.mock import MagicMock

from visitor_auth_byo_gateway import (
    is_direct_oauth_callback_query,
    should_forward_byo_callback_to_desktop,
    visitor_auth_handled_on_gateway,
)


def _request(method: str, query: dict[str, str] | None = None) -> MagicMock:
    req = MagicMock()
    req.method = method
    params = MagicMock()
    params.get = lambda k, d=None: (query or {}).get(k, d)
    req.query_params = params
    return req


class VisitorAuthByoGatewayTests(unittest.TestCase):
    def test_byo_forward_bare_callback_with_code_state(self):
        req = _request("GET", {"code": "c", "state": "s"})
        self.assertTrue(should_forward_byo_callback_to_desktop("visitor-auth/callback", req))
        self.assertFalse(visitor_auth_handled_on_gateway("visitor-auth/callback", req))

    def test_platform_callback_with_pending_id_on_gateway(self):
        req = _request("GET", {"code": "c"})
        path = "visitor-auth/callback/pending-abc"
        self.assertFalse(should_forward_byo_callback_to_desktop(path, req))
        self.assertTrue(visitor_auth_handled_on_gateway(path, req))

    def test_tools_stay_on_gateway(self):
        req = _request("POST")
        self.assertTrue(visitor_auth_handled_on_gateway("visitor-auth/tools/call", req))

    def test_direct_oauth_query_requires_both(self):
        self.assertFalse(is_direct_oauth_callback_query(_request("GET", {"code": "only"})))
        self.assertTrue(is_direct_oauth_callback_query(_request("GET", {"code": "c", "state": "s"})))


if __name__ == "__main__":
    unittest.main()

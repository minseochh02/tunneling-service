import unittest
from main import _sanitize_tunnel_response_headers


def test_sanitize_drops_chunked_and_sets_content_length():
    out = _sanitize_tunnel_response_headers(
        {
            "content-type": "text/html",
            "transfer-encoding": "chunked",
            "connection": "keep-alive",
            "content-length": "999",
        },
        120,
        "GET",
    )
    assert out["content-type"] == "text/html"
    assert "transfer-encoding" not in {k.lower() for k in out}
    assert out["content-length"] == "120"


def test_sanitize_head_keeps_origin_content_length():
    out = _sanitize_tunnel_response_headers(
        {"content-length": "555", "transfer-encoding": "chunked"},
        0,
        "HEAD",
    )
    assert out["content-length"] == "555"


class TunnelResponseHeaderTests(unittest.TestCase):
    def test_drops_chunked_and_sets_content_length(self):
        test_sanitize_drops_chunked_and_sets_content_length()

    def test_head_keeps_origin_content_length(self):
        test_sanitize_head_keeps_origin_content_length()


if __name__ == "__main__":
    unittest.main()

"""
When a tunnel socket is replaced by a newer one for the same tunnel id, requests that
were already sent over the OLD socket can never be answered (its client was told to
close). They must fail fast, while requests on the NEW socket stay pending.
"""
import asyncio
import json
import unittest

import main


class _FakeSocket:
    pass


class ReplacedSocketPendingRequestsTests(unittest.TestCase):
    def setUp(self) -> None:
        main.pending_requests.clear()
        main.pending_request_tunnels.clear()
        main.pending_request_sockets.clear()

    def tearDown(self) -> None:
        self.setUp()

    def _register(self, loop, request_id: str, tunnel_id: str, ws) -> asyncio.Future:
        fut = loop.create_future()
        main.pending_requests[request_id] = fut
        main.pending_request_tunnels[request_id] = tunnel_id
        main.pending_request_sockets[request_id] = ws
        return fut

    def test_only_requests_on_the_replaced_socket_fail(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            old_ws, new_ws = _FakeSocket(), _FakeSocket()
            old_fut = self._register(loop, "r-old", "mcp-server-fxkud1", old_ws)
            new_fut = self._register(loop, "r-new", "mcp-server-fxkud1", new_ws)

            failed = main.fail_pending_requests_for_tunnel(
                "mcp-server-fxkud1", reason="tunnel_replaced", websocket=old_ws
            )

            self.assertEqual(failed, 1)
            self.assertTrue(old_fut.done())
            self.assertEqual(old_fut.result()["status_code"], 503)
            body = json.loads(old_fut.result()["body"])
            self.assertEqual(body["error"], "tunnel_replaced")
            self.assertTrue(body["retryable"])
            self.assertFalse(new_fut.done())
            self.assertIn("r-new", main.pending_requests)
            self.assertNotIn("r-old", main.pending_request_sockets)
            self.assertIn("r-new", main.pending_request_sockets)
        finally:
            loop.close()

    def test_without_websocket_fails_every_request_for_the_tunnel(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            ws = _FakeSocket()
            a = self._register(loop, "r-a", "t1", ws)
            b = self._register(loop, "r-b", "t1", ws)
            other = self._register(loop, "r-c", "t2", ws)

            failed = main.fail_pending_requests_for_tunnel("t1")

            self.assertEqual(failed, 2)
            self.assertTrue(a.done() and b.done())
            self.assertFalse(other.done())
        finally:
            loop.close()


if __name__ == "__main__":
    unittest.main()

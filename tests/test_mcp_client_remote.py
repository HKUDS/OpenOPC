import asyncio
import unittest
from unittest.mock import patch

import mcp.client.streamable_http as streamable_http
import mcp.client.sse as sse

from opc.mcp_client import MCPRemoteConnection


class _AsyncContext:
    def __init__(self, value, events):
        self.value = value
        self.events = events

    async def __aenter__(self):
        self.events.append("enter")
        return self.value

    async def __aexit__(self, *args):
        self.events.append("exit")


class _Session:
    def __init__(self, read, write):
        self.serverInfo = {"name": "fixture", "version": "1"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def initialize(self):
        return self


class _FailingSession(_Session):
    async def initialize(self):
        raise RuntimeError("fixture initialize failed")


class RemoteMCPCompatibilityTests(unittest.TestCase):
    def test_current_sdk_uses_http_client_and_closes_it(self):
        events = []
        http_client = object()
        http_client_context = _AsyncContext(http_client, events)

        def current(url, *, http_client):
            self.assertEqual(url, "http://fixture/mcp")
            self.assertIs(http_client, expected_http_client)
            return _AsyncContext((object(), object()), events)

        def create_client(headers):
            self.assertEqual(headers, {"Authorization": "Bearer x"})
            return http_client_context

        expected_http_client = http_client
        with patch.object(streamable_http, "streamable_http_client", current), \
                patch.object(streamable_http, "create_mcp_http_client", create_client):
            with patch.object(streamable_http, "streamablehttp_client", None, create=True):
                with patch("opc.mcp_client.ClientSession", _Session):
                    conn = MCPRemoteConnection("fixture", "http://fixture/mcp", {"Authorization": "Bearer x"})
                    asyncio.run(conn.start())
                    asyncio.run(conn.stop())
        self.assertEqual(events, ["enter", "enter", "exit", "exit"])

    def test_legacy_sdk_receives_headers_directly(self):
        events = []
        seen = {}

        def legacy(url, headers=None):
            seen.update(url=url, headers=headers)
            return _AsyncContext((object(), object()), events)

        with patch.object(streamable_http, "streamablehttp_client", legacy, create=True), \
                patch.object(streamable_http, "streamable_http_client", None):
            with patch("opc.mcp_client.ClientSession", _Session):
                conn = MCPRemoteConnection("fixture", "http://fixture/mcp", {"X-Test": "yes"})
                asyncio.run(conn.start())
                asyncio.run(conn.stop())
        self.assertEqual(seen, {"url": "http://fixture/mcp", "headers": {"X-Test": "yes"}})
        self.assertEqual(events, ["enter", "exit"])

    def test_failed_sse_attempt_closes_entered_contexts(self):
        events = []

        def modern(url, *, http_client):
            return _AsyncContext((object(), object()), events)

        def sse_transport(url, headers=None):
            return _AsyncContext((object(), object()), events)

        with patch.object(streamable_http, "streamable_http_client", modern), \
                patch.object(streamable_http, "streamablehttp_client", None, create=True), \
                patch.object(streamable_http, "create_mcp_http_client", lambda headers: _AsyncContext(object(), events)), \
                patch.object(sse, "sse_client", sse_transport), \
                patch("opc.mcp_client.ClientSession", _FailingSession):
            conn = MCPRemoteConnection("fixture", "http://fixture/mcp")
            with self.assertRaises(ConnectionError):
                asyncio.run(conn.start())
        self.assertEqual(events, ["enter", "enter", "exit", "exit", "enter", "exit"])
        self.assertFalse(conn._exit_stack._exit_callbacks)


if __name__ == "__main__":
    unittest.main()

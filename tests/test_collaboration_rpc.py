from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from opc.layer4_tools.collaboration_rpc import (
    OPC_COLLAB_RPC_HOST,
    OPC_COLLAB_RPC_PATH,
    OPC_COLLAB_RPC_PORT,
    OPC_COLLAB_RPC_TOKEN,
    OPC_COLLAB_RPC_TRANSPORT,
    call_collaboration_rpc,
    resolve_collaboration_rpc_transport,
    rpc_env_available,
    rpc_env_configured,
    start_collaboration_rpc_server,
)


class CollaborationRpcTransportTests(unittest.IsolatedAsyncioTestCase):
    def test_rpc_env_available_supports_fifo_legacy_and_tcp_contracts(self) -> None:
        self.assertTrue(
            rpc_env_available(
                {
                    OPC_COLLAB_RPC_PATH: "/tmp/openopc/requests.fifo",
                    OPC_COLLAB_RPC_TOKEN: "token",
                }
            )
        )
        self.assertTrue(
            rpc_env_available(
                {
                    OPC_COLLAB_RPC_TRANSPORT: "fifo",
                    OPC_COLLAB_RPC_PATH: "/tmp/openopc/requests.fifo",
                    OPC_COLLAB_RPC_TOKEN: "token",
                }
            )
        )
        self.assertTrue(
            rpc_env_available(
                {
                    OPC_COLLAB_RPC_TRANSPORT: "tcp",
                    OPC_COLLAB_RPC_HOST: "127.0.0.1",
                    OPC_COLLAB_RPC_PORT: "49321",
                    OPC_COLLAB_RPC_TOKEN: "token",
                }
            )
        )
        self.assertFalse(
            rpc_env_available(
                {
                    OPC_COLLAB_RPC_TRANSPORT: "tcp",
                    OPC_COLLAB_RPC_HOST: "127.0.0.1",
                    OPC_COLLAB_RPC_TOKEN: "token",
                }
            )
        )
        self.assertFalse(rpc_env_configured({}))
        self.assertTrue(rpc_env_configured({OPC_COLLAB_RPC_TRANSPORT: "tcp"}))

    async def test_collab_cli_does_not_fallback_when_rpc_env_is_malformed(self) -> None:
        from opc import cli_collab

        with patch.dict(
            "os.environ",
            {
                OPC_COLLAB_RPC_TRANSPORT: "unknown",
                OPC_COLLAB_RPC_TOKEN: "token",
            },
            clear=True,
        ), patch(
            "opc.layer4_tools.collaboration_dispatch.dispatch_collaboration_tool",
            side_effect=AssertionError("direct dispatch should not run when RPC env is configured"),
        ):
            payload, is_error = await cli_collab._dispatch("manager_board_read", {})

        self.assertTrue(is_error)
        self.assertEqual(payload["error_type"], "infrastructure")
        self.assertIn("transport is unsupported", payload["error"])
        self.assertFalse(
            rpc_env_available(
                {
                    OPC_COLLAB_RPC_TRANSPORT: "unknown",
                    OPC_COLLAB_RPC_TOKEN: "token",
                }
            )
        )

    def test_collab_cli_rejects_inline_args_json_on_windows_external_rpc_env(self) -> None:
        from opc import cli_collab

        parser = cli_collab._build_parser()
        opts = parser.parse_args(["delegate_work", "--args-json", '{"items": []}'])

        with patch.object(cli_collab.os, "name", "nt"), patch.dict(
            "os.environ",
            {OPC_COLLAB_RPC_TRANSPORT: "tcp"},
            clear=True,
        ):
            with self.assertRaises(SystemExit) as raised:
                cli_collab._collect_tool_args(opts)

        self.assertIn("--args-json is disabled on Windows", str(raised.exception))

    async def test_collab_cli_args_json_file_preserves_unicode_on_windows_rpc_env(self) -> None:
        from opc import cli_collab

        title = "\u8d22\u52a1\u3001\u4f30\u503c\u4e0e 10x \u53ef\u884c\u6027\u7b5b\u9009"
        seen: list[dict[str, object]] = []

        async def _dispatch(tool_name: str, args: dict[str, object]):
            seen.append({"tool": tool_name, "args": dict(args)})
            return {"ok": True}, False

        server = await start_collaboration_rpc_server(_dispatch, transport="tcp")
        self.assertIsNotNone(server)
        assert server is not None
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                args_path = Path(tmpdir) / "args.json"
                args_path.write_text(
                    json.dumps(
                        {"items": [{"role_id": "cmo", "title": title}]},
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                parser = cli_collab._build_parser()
                opts = parser.parse_args(["delegate_work", "--args-json-file", str(args_path)])
                # Simulate os.name == "nt" only at the product's single decision
                # point instead of patching the global os.name, which would turn
                # every pathlib.Path in this process into a WindowsPath and break
                # the POSIX file reads the test itself performs. The real
                # rpc_env_configured() check still runs against the patched env,
                # so the Windows guard branch is exercised end to end.
                with patch.object(
                    cli_collab,
                    "_windows_external_rpc_env_configured",
                    lambda: rpc_env_configured(),
                ), patch.dict(
                    "os.environ",
                    server.client_env,
                    clear=True,
                ):
                    self.assertTrue(cli_collab._windows_external_rpc_env_configured())
                    tool_args = cli_collab._collect_tool_args(opts)
                    payload, is_error = await cli_collab._dispatch(opts.tool, tool_args)
        finally:
            await server.close()

        self.assertFalse(is_error)
        self.assertEqual(payload["ok"], True)
        self.assertEqual(seen[0]["tool"], "delegate_work")
        received = seen[0]["args"]["items"][0]["title"]  # type: ignore[index]
        self.assertEqual(received, title)

    def test_collab_cli_rejects_args_stdin_on_windows_external_rpc_env(self) -> None:
        from opc import cli_collab

        parser = cli_collab._build_parser()
        opts = parser.parse_args(["delegate_work", "--args-stdin"])

        with patch.object(cli_collab.os, "name", "nt"), patch.dict(
            "os.environ",
            {OPC_COLLAB_RPC_TRANSPORT: "tcp"},
            clear=True,
        ):
            with self.assertRaises(SystemExit) as raised:
                cli_collab._collect_tool_args(opts)

        self.assertIn("--args-stdin is disabled on Windows", str(raised.exception))

    def test_collab_cli_args_json_file_accepts_utf8_sig(self) -> None:
        from opc import cli_collab

        title = "\u8d22\u52a1\u3001\u4f30\u503c\u4e0e 10x \u53ef\u884c\u6027\u7b5b\u9009"
        parser = cli_collab._build_parser()

        with tempfile.TemporaryDirectory() as tmpdir:
            args_path = Path(tmpdir) / "args.json"
            args_path.write_text(
                json.dumps(
                    {"items": [{"role_id": "cmo", "title": title}]},
                    ensure_ascii=False,
                ),
                encoding="utf-8-sig",
            )
            opts = parser.parse_args(["delegate_work", "--args-json-file", str(args_path)])
            payload = cli_collab._collect_tool_args(opts)

        self.assertEqual(payload["items"][0]["title"], title)

    def test_forced_fifo_transport_fails_cleanly_when_unavailable(self) -> None:
        with patch("opc.layer4_tools.collaboration_rpc.fifo_rpc_supported", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "FIFO collaboration RPC is unavailable"):
                resolve_collaboration_rpc_transport("fifo")

    async def test_fifo_rpc_call_fails_cleanly_when_platform_has_no_fifo(self) -> None:
        with patch("opc.layer4_tools.collaboration_rpc.fifo_rpc_supported", return_value=False), patch(
            "opc.layer4_tools.collaboration_rpc.os.mkfifo",
            side_effect=AssertionError("mkfifo should not be called"),
            create=True,
        ):
            payload, is_error = await call_collaboration_rpc(
                "manager_board_read",
                {"include_children": True},
                env={
                    OPC_COLLAB_RPC_TRANSPORT: "fifo",
                    OPC_COLLAB_RPC_PATH: "/tmp/openopc/requests.fifo",
                    OPC_COLLAB_RPC_TOKEN: "token",
                },
            )

        self.assertTrue(is_error)
        self.assertEqual(payload["error_type"], "infrastructure")
        self.assertIn("FIFO collaboration broker RPC is unavailable", payload["error"])

    async def test_auto_transport_uses_tcp_when_fifo_unavailable(self) -> None:
        async def _dispatch(tool_name: str, args: dict[str, object]):
            return {"tool": tool_name, "args": args}, False

        with patch("opc.layer4_tools.collaboration_rpc.fifo_rpc_supported", return_value=False), patch(
            "opc.layer4_tools.collaboration_rpc.os.mkfifo",
            side_effect=AssertionError("mkfifo should not be called"),
            create=True,
        ):
            server = await start_collaboration_rpc_server(_dispatch)

        self.assertIsNotNone(server)
        assert server is not None
        try:
            self.assertEqual(server.transport, "tcp")
            self.assertEqual(server.client_env[OPC_COLLAB_RPC_TRANSPORT], "tcp")
            self.assertEqual(server.client_env[OPC_COLLAB_RPC_HOST], "127.0.0.1")
            self.assertTrue(int(server.client_env[OPC_COLLAB_RPC_PORT]) > 0)
        finally:
            await server.close()

    async def test_tcp_rpc_roundtrip_routes_to_dispatch(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        async def _dispatch(tool_name: str, args: dict[str, object]):
            calls.append((tool_name, dict(args)))
            return {"ok": True, "tool": tool_name, "args": args}, False

        server = await start_collaboration_rpc_server(_dispatch, transport="tcp")
        self.assertIsNotNone(server)
        assert server is not None
        try:
            payload, is_error = await call_collaboration_rpc(
                "delegate_work",
                {"items": [{"role_id": "cto", "title": "Build"}]},
                env=server.client_env,
            )
        finally:
            await server.close()

        self.assertFalse(is_error)
        self.assertEqual(payload["tool"], "delegate_work")
        self.assertEqual([name for name, _args in calls], ["delegate_work"])

    async def test_tcp_rpc_invalid_token_returns_typed_error(self) -> None:
        async def _dispatch(tool_name: str, args: dict[str, object]):
            return {"should_not_run": True}, False

        server = await start_collaboration_rpc_server(_dispatch, transport="tcp")
        self.assertIsNotNone(server)
        assert server is not None
        try:
            env = dict(server.client_env)
            env[OPC_COLLAB_RPC_TOKEN] = "wrong-token"
            payload, is_error = await call_collaboration_rpc(
                "send_dm",
                {"to_agent": "cto"},
                env=env,
            )
        finally:
            await server.close()

        self.assertTrue(is_error)
        self.assertEqual(payload["error_type"], "infrastructure")
        self.assertIn("token rejected", payload["error"])
        self.assertEqual(payload["tool_name"], "send_dm")

    async def test_tcp_rpc_malformed_json_returns_infrastructure_error(self) -> None:
        async def _dispatch(tool_name: str, args: dict[str, object]):
            return {"should_not_run": True}, False

        server = await start_collaboration_rpc_server(_dispatch, transport="tcp")
        self.assertIsNotNone(server)
        assert server is not None
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.open_connection(server.host, server.port)
            writer.write(b"{not-json}\n")
            await writer.drain()
            raw = await reader.readline()
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            await server.close()

        response = json.loads(raw.decode("utf-8"))
        self.assertTrue(response["is_error"])
        self.assertEqual(response["result"]["error_type"], "infrastructure")
        self.assertIn("invalid JSON", response["result"]["error"])


if __name__ == "__main__":
    unittest.main()

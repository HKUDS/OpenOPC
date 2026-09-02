from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from opc.layer3_agent.jiuwen_gateway import (
    DEFAULT_JIUWEN_GATEWAY_URL,
    probe_jiuwen_gateway,
    resolve_jiuwen_gateway_url,
)
from opc.layer3_agent.jiuwen_gateway_runner import _install_cancel_handlers


def test_gateway_resolver_reads_jiuwen_persisted_fallback_port(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".env").write_text(
        'GATEWAY_HOST="0.0.0.0"\nGATEWAY_PORT="20001"\nAPI_KEY="ignored"\n',
        encoding="utf-8",
    )
    with (
        patch.dict(
            os.environ,
            {"JIUWENSWARM_DATA_DIR": str(tmp_path)},
            clear=False,
        ),
        patch.dict(
            os.environ,
            {"JIUWENSWARM_GATEWAY_URL": "", "GATEWAY_HOST": "", "GATEWAY_PORT": ""},
            clear=False,
        ),
    ):
        assert resolve_jiuwen_gateway_url(DEFAULT_JIUWEN_GATEWAY_URL) == (
            "ws://127.0.0.1:20001/tui"
        )


def test_gateway_resolver_keeps_explicit_openopc_override(tmp_path: Path) -> None:
    with patch.dict(
        os.environ,
        {
            "JIUWENSWARM_DATA_DIR": str(tmp_path),
            "JIUWENSWARM_GATEWAY_URL": "",
            "GATEWAY_HOST": "",
            "GATEWAY_PORT": "",
        },
        clear=False,
    ):
        assert resolve_jiuwen_gateway_url("wss://gateway.example.test/tui") == (
            "wss://gateway.example.test/tui"
        )


def test_gateway_probe_requires_public_connection_ack() -> None:
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.recv.return_value = json.dumps(
        {"type": "event", "event": "connection.ack", "payload": {}}
    )
    with patch("websockets.sync.client.connect", return_value=connection):
        assert probe_jiuwen_gateway(DEFAULT_JIUWEN_GATEWAY_URL) == ""

    connection.recv.return_value = json.dumps({"type": "event", "event": "wrong"})
    with patch("websockets.sync.client.connect", return_value=connection):
        assert "unexpected handshake" in probe_jiuwen_gateway(
            DEFAULT_JIUWEN_GATEWAY_URL
        )


def test_gateway_runner_installs_sync_signal_fallback() -> None:
    loop = MagicMock()
    loop.add_signal_handler.side_effect = NotImplementedError
    cancelled = MagicMock()
    with (
        patch(
            "opc.layer3_agent.jiuwen_gateway_runner.signal.getsignal",
            return_value="previous",
        ),
        patch("opc.layer3_agent.jiuwen_gateway_runner.signal.signal") as install,
    ):
        handlers = _install_cancel_handlers(loop, cancelled)

    assert handlers
    assert all(kind == "sync" for kind, _, _ in handlers)
    callback = install.call_args_list[0].args[1]
    callback()
    loop.call_soon_threadsafe.assert_called_with(cancelled.set)


class GatewayRunnerProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_runner_roundtrip_preserves_unicode_team_mode(self) -> None:
        from websockets.asyncio.server import serve

        received: list[dict[str, object]] = []

        async def _gateway(websocket: object) -> None:
            await websocket.send(  # type: ignore[attr-defined]
                json.dumps({"type": "event", "event": "connection.ack", "payload": {}})
            )
            request = json.loads(await websocket.recv())  # type: ignore[attr-defined]
            received.append(request)
            await websocket.send(  # type: ignore[attr-defined]
                json.dumps(
                    {
                        "type": "event",
                        "event": "chat.final",
                        "payload": {"event_type": "chat.final", "content": "完成"},
                    },
                    ensure_ascii=False,
                )
            )
            await websocket.send(  # type: ignore[attr-defined]
                json.dumps(
                    {
                        "type": "event",
                        "event": "chat.processing_status",
                        "payload": {"is_processing": False},
                    }
                )
            )

        try:
            server = await serve(_gateway, "127.0.0.1", 0)
        except OSError as exc:
            self.skipTest(f"loopback sockets unavailable in this sandbox: {exc}")
        try:
            port = int(server.sockets[0].getsockname()[1])
            with tempfile.TemporaryDirectory() as temp_dir:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "opc.layer3_agent.jiuwen_gateway_runner",
                    "--gateway-url",
                    f"ws://127.0.0.1:{port}/tui",
                    "--mode",
                    "team",
                    "--session",
                    "跨平台-session",
                    "--cwd",
                    temp_dir,
                    "--project-dir",
                    temp_dir,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(
                        (
                            json.dumps(
                                {"type": "start", "prompt": "完成跨平台调研"},
                                ensure_ascii=False,
                            )
                            + "\n"
                        ).encode("utf-8")
                    ),
                    timeout=10,
                )
        finally:
            server.close()
            await server.wait_closed()

        self.assertEqual(
            process.returncode, 0, stderr.decode("utf-8", errors="replace")
        )
        frames = [json.loads(line) for line in stdout.decode("utf-8").splitlines()]
        self.assertTrue(any(frame.get("event") == "chat.final" for frame in frames))
        self.assertEqual(received[0]["method"], "chat.send")
        self.assertEqual(received[0]["params"]["mode"], "team")  # type: ignore[index]
        self.assertEqual(received[0]["params"]["content"], "完成跨平台调研")  # type: ignore[index]

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_BOOTSTRAP = "from opc.cli.app import main; main()"
SYNTHETIC_KEY_NAME = "OPENOPC_WORKSPACE_TRUST_E2E_KEY"
SYNTHETIC_KEY_VALUE = "synthetic-loopback-key"


class _CaptureHandler(BaseHTTPRequestHandler):
    server: "_CaptureServer"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length)
        self.server.requests.append(
            {
                "path": self.path,
                "authorization": self.headers.get("authorization", ""),
                "body": body.decode("utf-8", errors="replace"),
            }
        )
        payload = json.dumps(
            {
                "id": "chatcmpl-workspace-trust",
                "object": "chat.completion",
                "created": 0,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        return


class _CaptureServer(ThreadingHTTPServer):
    requests: list[dict[str, str]]


class LoopbackLLM:
    def __init__(self) -> None:
        self.server = _CaptureServer(("127.0.0.1", 0), _CaptureHandler)
        self.server.requests = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def api_base(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}/v1"

    @property
    def requests(self) -> list[dict[str, str]]:
        return self.server.requests

    def __enter__(self) -> "LoopbackLLM":
        self.thread.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _base_environment(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("OPC_HOME", None)
    env["XDG_CONFIG_HOME"] = str(tmp_path / "user-config")
    env["APPDATA"] = str(tmp_path / "user-config")
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(REPO_ROOT), env.get("PYTHONPATH", "")) if part
    )
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = env["NO_PROXY"]
    env[SYNTHETIC_KEY_NAME] = SYNTHETIC_KEY_VALUE
    return env


def _run_cli(
    workspace: Path,
    env: dict[str, str],
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", CLI_BOOTSTRAP, *arguments],
        cwd=workspace,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _write_workspace(workspace: Path, api_base: str) -> Path:
    workspace.mkdir(parents=True)
    (workspace / "pyproject.toml").write_text(
        "[project]\nname = 'workspace-trust-e2e'\nversion = '0'\n",
        encoding="utf-8",
    )
    config_dir = workspace / ".opc" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "llm_config.yaml").write_text(
        "llm:\n"
        "  default_model: openai/test-model\n"
        f"  api_base: {api_base}\n"
        f"  api_key_env: {SYNTHETIC_KEY_NAME}\n"
        "  max_tokens: 16\n",
        encoding="utf-8",
    )
    return config_dir


def _trust_workspace(workspace: Path, env: dict[str, str]) -> None:
    result = _run_cli(workspace, env, "trust", "add", str(workspace))
    assert result.returncode == 0, result.stdout + result.stderr


def test_untrusted_cli_never_starts_project_local_mcp(tmp_path: Path) -> None:
    workspace = tmp_path / "mcp-victim"
    marker = tmp_path / "mcp-started"
    probe = workspace / "mcp_probe.py"
    env = _base_environment(tmp_path)

    with LoopbackLLM() as llm:
        config_dir = _write_workspace(workspace, llm.api_base)
        probe.write_text(
            "import json\n"
            "from pathlib import Path\n"
            "import sys\n"
            f"Path({str(marker)!r}).write_text('started\\n', encoding='utf-8')\n"
            "for line in sys.stdin:\n"
            "    request = json.loads(line)\n"
            "    if 'id' not in request:\n"
            "        continue\n"
            "    method = request.get('method')\n"
            "    if method == 'initialize':\n"
            "        result = {\n"
            "            'protocolVersion': request.get('params', {}).get('protocolVersion', '2024-11-05'),\n"
            "            'capabilities': {'tools': {}},\n"
            "            'serverInfo': {'name': 'workspace-trust-probe', 'version': '1'},\n"
            "        }\n"
            "    elif method == 'tools/list':\n"
            "        result = {'tools': []}\n"
            "    else:\n"
            "        result = {}\n"
            "    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)\n",
            encoding="utf-8",
        )
        (config_dir / "system_config.yaml").write_text(
            "system: {}\n"
            "mcp_servers:\n"
            "  - name: marker\n"
            "    type: local\n"
            f"    command: [{json.dumps(sys.executable)}, {json.dumps(str(probe))}]\n"
            "    enabled: true\n"
            "    startup_timeout: 10\n",
            encoding="utf-8",
        )

        blocked = _run_cli(workspace, env, "chat", "marker trigger", "--no-markdown")

        assert blocked.returncode == 2, blocked.stdout + blocked.stderr
        assert not marker.exists()
        assert llm.requests == []

        _trust_workspace(workspace, env)
        allowed = _run_cli(workspace, env, "chat", "marker trigger", "--no-markdown")

    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    assert marker.read_text(encoding="utf-8") == "started\n"


def test_untrusted_cli_never_routes_credentials_to_project_llm(tmp_path: Path) -> None:
    workspace = tmp_path / "llm-victim"
    env = _base_environment(tmp_path)

    with LoopbackLLM() as llm:
        _write_workspace(workspace, llm.api_base)

        blocked = _run_cli(workspace, env, "chat", "credential trigger", "--no-markdown")

        assert blocked.returncode == 2, blocked.stdout + blocked.stderr
        assert llm.requests == []

        _trust_workspace(workspace, env)
        allowed = _run_cli(workspace, env, "chat", "credential trigger", "--no-markdown")

        assert allowed.returncode == 0, allowed.stdout + allowed.stderr
        assert len(llm.requests) == 1
        request = llm.requests[0]
        assert request["path"].endswith("/chat/completions")
        assert request["authorization"] == f"Bearer {SYNTHETIC_KEY_VALUE}"
        assert "credential trigger" in request["body"]

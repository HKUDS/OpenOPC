"""Shared Jiuwen Gateway endpoint resolution and protocol preflight."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_JIUWEN_GATEWAY_URL = "ws://127.0.0.1:19001/tui"


def _dotenv_gateway_values() -> dict[str, str]:
    data_root = str(os.environ.get("JIUWENSWARM_DATA_DIR") or "").strip()
    if data_root:
        env_path = Path(data_root).expanduser() / "config" / ".env"
    else:
        user_home = Path(
            str(os.environ.get("JIUWENSWARM_HOME") or "").strip() or Path.home()
        ).expanduser()
        env_path = user_home / ".jiuwenswarm" / "config" / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or key not in {"GATEWAY_HOST", "GATEWAY_PORT"}:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value.strip()
    return values


def resolve_jiuwen_gateway_url(configured: str = "") -> str:
    """Resolve the live Gateway without importing Jiuwen's Python package."""

    explicit_env = str(os.environ.get("JIUWENSWARM_GATEWAY_URL") or "").strip()
    if explicit_env:
        return explicit_env
    configured_value = str(configured or "").strip()
    # A non-default OpenOPC value is an intentional override.  The shipped
    # default is weak so Jiuwen's own persisted custom port can be honored.
    if configured_value and configured_value != DEFAULT_JIUWEN_GATEWAY_URL:
        return configured_value
    gateway_host = str(os.environ.get("GATEWAY_HOST") or "").strip()
    gateway_port = str(os.environ.get("GATEWAY_PORT") or "").strip()
    persisted = _dotenv_gateway_values()
    host = gateway_host or persisted.get("GATEWAY_HOST", "") or "127.0.0.1"
    port = gateway_port or persisted.get("GATEWAY_PORT", "") or "19001"
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1" if host == "0.0.0.0" else "::1"
    try:
        numeric_port = int(port)
    except (TypeError, ValueError):
        return configured_value or DEFAULT_JIUWEN_GATEWAY_URL
    if not 1 <= numeric_port <= 65535:
        return configured_value or DEFAULT_JIUWEN_GATEWAY_URL
    rendered_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"ws://{rendered_host}:{numeric_port}/tui"


def probe_jiuwen_gateway(url: str, *, timeout: float = 2.0) -> str:
    """Complete a WebSocket handshake and require Jiuwen's public ack event."""

    value = str(url or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
        return f"invalid Jiuwen Gateway URL: {url!r}"
    try:
        from websockets.sync.client import connect
    except ImportError:
        return "Jiuwen Gateway preflight requires websockets>=12.0"
    try:
        with connect(
            value,
            open_timeout=timeout,
            close_timeout=timeout,
            max_size=8 * 2**20,
        ) as connection:
            raw = connection.recv(timeout=timeout)
            payload = json.loads(raw)
    except Exception as exc:
        return f"Jiuwen Gateway protocol is unavailable at {value}: {exc}"
    if not isinstance(payload, dict) or not (
        payload.get("type") == "event" and payload.get("event") == "connection.ack"
    ):
        return f"Jiuwen Gateway returned an unexpected handshake at {value}"
    return ""

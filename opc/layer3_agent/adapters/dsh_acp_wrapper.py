"""dsh ACP wrapper — bridges dsh's ACP stdio server into OpenOPC's line protocol.

The external broker drives external agents as one subprocess per task whose
stdout is read line by line and whose stdin accepts approval responses. dsh's
ACP server is a long-lived JSON-RPC stdio process, so this wrapper sits
between them: it spawns ``dsh --profile acp``, performs the ``session/new`` +
``session/prompt`` handshake, forwards semantic progress and permission
requests as JSON lines on stdout, relays the broker's approval decisions back
over ACP, and exits when the prompt settles — restoring the broker's
"process exit marks completion" contract.

Wrapper stdout protocol (one JSON object per line):

- ``{"type": "progress", "text": "..."}`` — a semantic progress update.
- ``{"type": "approval", "callId": <int>, "toolName": "...", "arguments": {...},
   "promptText": "..."}`` — an ACP ``session/request_permission``; the broker
  answers with one approval-response line on stdin.
- ``{"type": "session", "id": "..."}`` — the created/resumed ACP session id.

Wrapper stdin protocol:

- ``{"type": "approval_response", "callId": <int>, "allowed": true|false}``

The wrapper itself never times out: the broker owns idle/hard timeouts and
terminates this process when they fire.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
from typing import Any

_DEFAULT_DSH_CMD = "dsh"
_ACP_PROFILE = "acp"
_NEW_SESSION_ID = 1
_PROMPT_ID = 2

_ALLOW_ONCE = "allow-once"
_REJECT_ONCE = "reject-once"


def _emit(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _extract_update_text(update: dict[str, Any]) -> str:
    """Concatenate committed text blocks from a dsh ``session/update`` payload.

    dsh emits ``content`` both as a single block object (agent_thought_chunk /
    agent_message_chunk) and as a list, so accept either shape.
    """
    content = update.get("content")
    if isinstance(content, list):
        blocks = content
    elif isinstance(content, dict):
        blocks = [content]
    else:
        return ""
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n".join(parts)


async def _write_json(writer: asyncio.StreamWriter, obj: dict[str, Any]) -> None:
    writer.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
    await writer.drain()


async def _read_json_lines(reader: asyncio.StreamReader):
    while True:
        line = await reader.readline()
        if not line:
            return
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            yield json.loads(text)
        except json.JSONDecodeError:
            continue


async def _rpc(
    writer: asyncio.StreamWriter,
    reader: asyncio.StreamReader,
    request_id: int,
    method: str,
    params: dict[str, Any],
) -> dict[str, Any] | None:
    await _write_json(writer, {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    async for message in _read_json_lines(reader):
        if message.get("id") == request_id:
            return message
    return None


def _build_outcome(allowed: bool) -> dict[str, Any]:
    option_id = _ALLOW_ONCE if allowed else _REJECT_ONCE
    outcome = "allowed" if allowed else "rejected"
    return {"outcome": outcome, "optionId": option_id}


async def _wait_approval_response(
    queue: asyncio.Queue[dict[str, Any] | None],
    call_id: int,
) -> bool:
    while True:
        message = await queue.get()
        if message is None:
            # stdin closed before a response arrived; reject rather than wait.
            return False
        if not isinstance(message, dict):
            continue
        if message.get("type") != "approval_response":
            continue
        if str(message.get("callId") or "") != str(call_id):
            continue
        return bool(message.get("allowed"))


async def _approval_message_text(params: dict[str, Any]) -> str:
    tool_call = params.get("toolCall") or {}
    tool_id = str(tool_call.get("toolCallId") or "")
    return f"dsh requested tool permission (call {tool_id})"


async def _run(
    dsh_cmd: str,
    cwd: str,
    prompt: str,
    resume_session_id: str,
    approval: str,
) -> int:
    env = dict(os.environ)
    proc = await asyncio.create_subprocess_exec(
        dsh_cmd,
        "--profile",
        _ACP_PROFILE,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
    )
    if proc.stdin is None or proc.stdout is None:
        return 2
    writer, reader = proc.stdin, proc.stdout

    async def _relay_stderr() -> None:
        assert proc.stderr is not None
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                return
            sys.stderr.buffer.write(chunk)
            sys.stderr.buffer.flush()

    stderr_task = asyncio.create_task(_relay_stderr())
    incoming_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _deliver(message: dict[str, Any] | None) -> None:
        # asyncio.Queue is not thread-safe; always enqueue on the event loop.
        incoming_queue.put_nowait(message)

    def _read_stdin_sync() -> None:
        # Raw os.read never takes the BufferedReader lock, so a daemon thread
        # blocked here cannot deadlock interpreter shutdown. Threaded reads
        # work for any stdin fd (pipe, tty, /dev/null).
        buffer = b""
        try:
            while True:
                chunk = os.read(0, 65536)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    text = line.decode("utf-8", errors="replace").strip()
                    if not text:
                        continue
                    try:
                        loop.call_soon_threadsafe(_deliver, json.loads(text))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass
        finally:
            try:
                loop.call_soon_threadsafe(_deliver, None)  # EOF: no more responses
            except RuntimeError:
                pass

    # Daemon thread: when stdin never reaches EOF (broker keeps the pipe open)
    # the interpreter must still exit after the prompt settles.
    stdin_task = threading.Thread(target=_read_stdin_sync, name="dsh-wrapper-stdin", daemon=True)
    stdin_task.start()

    try:
        if resume_session_id:
            response = await _rpc(writer, reader, _NEW_SESSION_ID, "session/resume", {"sessionId": resume_session_id, "cwd": cwd, "mcpServers": []})
            if response is None or "error" in response:
                _emit({"type": "fatal", "reason": "session handshake failed", "detail": response})
                return 2
            # dsh's session/resume result carries no sessionId; the resumed
            # session keeps the requested id.
            session_id = resume_session_id
        else:
            response = await _rpc(writer, reader, _NEW_SESSION_ID, "session/new", {"cwd": cwd, "mcpServers": []})
            if response is None or "error" in response:
                _emit({"type": "fatal", "reason": "session handshake failed", "detail": response})
                return 2
            result = response.get("result") or {}
            session_id = str(result.get("sessionId") or "")
            if not session_id:
                _emit({"type": "fatal", "reason": "session/new returned no sessionId"})
                return 2
        _emit({"type": "session", "id": session_id})

        await _write_json(
            writer,
            {
                "jsonrpc": "2.0",
                "id": _PROMPT_ID,
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": prompt}],
                },
            },
        )

        async for message in _read_json_lines(reader):
            if message.get("id") == _PROMPT_ID:
                if "error" in message:
                    _emit({"type": "fatal", "reason": "session/prompt failed", "detail": message.get("error")})
                    return 2
                return 0
            method = str(message.get("method") or "")
            if method == "session/request_permission":
                call_id = message.get("id")
                params = dict(message.get("params") or {})
                tool_call = dict(params.get("toolCall") or {})
                tool_name = str(tool_call.get("name") or "dsh_tool_call")
                arguments = dict(tool_call.get("arguments") or {})
                _emit(
                    {
                        "type": "approval",
                        "callId": call_id,
                        "toolName": tool_name,
                        "arguments": arguments,
                        "promptText": await _approval_message_text(params),
                    }
                )
                allowed = approval == "auto"
                if approval == "bridge":
                    # EOF (None) inside _wait_approval_response rejects.
                    allowed = await _wait_approval_response(incoming_queue, call_id)
                await _write_json(
                    writer,
                    {"jsonrpc": "2.0", "id": call_id, "result": _build_outcome(allowed)},
                )
            elif method == "session/update":
                params = dict(message.get("params") or {})
                update = dict(params.get("update") or {})
                text = _extract_update_text(update)
                if text:
                    _emit({"type": "progress", "text": text})
        return 0
    finally:
        stderr_task.cancel()
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="dsh-acp-wrapper", description=__doc__)
    parser.add_argument("--dsh-cmd", default="", help="dsh executable path (default: $DSH_CMD or 'dsh')")
    parser.add_argument("--cwd", required=True, help="workspace the dsh agent runs in")
    parser.add_argument("--resume", default="", help="resume an existing ACP session id")
    parser.add_argument(
        "--approval",
        choices=("bridge", "auto", "reject"),
        default="bridge",
        help="bridge: relay decisions to OpenOPC (broker path); auto/reject: decide locally (headless execute path)",
    )
    parser.add_argument("prompt", help="the task prompt for the dsh agent")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(argv) if argv is not None else sys.argv[1:])
    dsh_cmd = str(args.dsh_cmd or os.environ.get("DSH_CMD") or _DEFAULT_DSH_CMD).strip() or _DEFAULT_DSH_CMD
    try:
        return asyncio.run(_run(dsh_cmd, args.cwd, args.prompt, args.resume, args.approval))
    except FileNotFoundError:
        sys.stderr.write(f"dsh-acp-wrapper: executable not found: {dsh_cmd}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

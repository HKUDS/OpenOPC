"""Small stdio-to-Jiuwen Gateway bridge used by the external broker.

The bridge deliberately depends only on Jiuwen's public ``/tui`` WebSocket
protocol.  OpenOPC and Jiuwen therefore keep independent Python environments,
while the broker still gets JSONL progress, durable session ids, HITL replies,
and a graceful ``chat.interrupt`` path.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import sys
import uuid
from pathlib import Path
from typing import Any


_INTERRUPT_RESUME_SOURCES = frozenset(
    {
        "confirm_interrupt",
        "permission_interrupt",
        "ask_user_interrupt",
        "evolution_interrupt",
    }
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--gateway-url", default="")
    parser.add_argument("--mode", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--trusted-dir", action="append", default=[])
    parser.add_argument("--agent-group-name", default="")
    parser.add_argument("--timeout", type=float, default=0.0)
    return parser


def _gateway_url(configured: str) -> str:
    value = str(configured or os.environ.get("JIUWENSWARM_GATEWAY_URL") or "").strip()
    if value:
        return value
    host = os.environ.get("GATEWAY_HOST", "127.0.0.1")
    port = os.environ.get("GATEWAY_PORT", "19001")
    return f"ws://{host}:{port}/tui"


def _emit(frame: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(frame, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


async def _stdin_json() -> dict[str, Any]:
    line = await asyncio.to_thread(sys.stdin.readline)
    if not line:
        return {}
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return {"value": line.strip()}
    return value if isinstance(value, dict) else {"value": value}


def _is_team_mode(mode: str) -> bool:
    normalized = str(mode or "").strip().lower()
    return "team" in normalized.split(".")


def _is_terminal(event_type: str, payload: dict[str, Any], *, team_mode: bool) -> tuple[bool, int]:
    if event_type == "chat.error":
        return True, 1
    if event_type == "chat.final" and str(payload.get("event_type") or "") == "team.error":
        return True, 1
    if event_type == "chat.processing_status" and payload.get("is_processing", True) is False:
        return True, 0
    if event_type == "chat.final":
        inner = str(payload.get("event_type") or "")
        content_final = not inner or inner == "chat.final"
        if content_final and not team_mode:
            return True, 0
    return False, 0


async def _connect(url: str) -> Any:
    try:
        from websockets.legacy.client import connect
    except ImportError:  # websockets >= 14 keeps a compatibility import path optional
        from websockets import connect
    return await connect(
        url,
        close_timeout=2.0,
        max_size=8 * 2**20,
        ping_interval=20,
        ping_timeout=60,
    )


async def _send(ws: Any, frame: dict[str, Any]) -> None:
    await ws.send(json.dumps(frame, ensure_ascii=False, default=str))


async def _answer_interaction(
    ws: Any,
    *,
    session_id: str,
    mode: str,
    payload: dict[str, Any],
) -> bool:
    response = await _stdin_json()
    if str(response.get("type") or "") == "cancel":
        return False
    selected = str(response.get("selected") or response.get("answer") or "").strip()
    custom_input = str(response.get("custom_input") or selected).strip()
    if not selected:
        selected = "reject"
    answers = [{"selected_options": [selected], "custom_input": custom_input}]
    request_id = str(payload.get("request_id") or "").strip()
    source = str(payload.get("source") or "").strip()
    if source in _INTERRUPT_RESUME_SOURCES and request_id:
        frame = {
            "type": "req",
            "id": f"answer-{uuid.uuid4().hex[:12]}",
            "method": "chat.send",
            "is_stream": True,
            "params": {
                "session_id": session_id,
                "query": "",
                "content": "",
                "request_id": request_id,
                "answers": answers,
                "source": source,
                "mode": mode,
                "supports_user_interaction": True,
            },
        }
    else:
        frame = {
            "type": "req",
            "id": f"answer-{uuid.uuid4().hex[:12]}",
            "method": "chat.user_answer",
            "is_stream": False,
            "params": {
                "session_id": session_id,
                "answers": answers,
                "request_id": request_id,
            },
        }
    await _send(ws, frame)
    return True


async def _interrupt(ws: Any, session_id: str) -> None:
    with contextlib.suppress(Exception):
        await _send(
            ws,
            {
                "type": "req",
                "id": f"interrupt-{uuid.uuid4().hex[:12]}",
                "method": "chat.interrupt",
                "is_stream": False,
                "params": {"session_id": session_id},
            },
        )


async def _main(args: argparse.Namespace) -> int:
    initial = await _stdin_json()
    prompt = str(initial.get("prompt") or initial.get("content") or "").strip()
    if not prompt:
        _emit({"type": "event", "event": "chat.error", "payload": {"error": "empty OpenOPC prompt"}})
        return 2

    url = _gateway_url(args.gateway_url)
    try:
        ws = await asyncio.wait_for(_connect(url), timeout=10.0)
    except Exception as exc:
        _emit(
            {
                "type": "event",
                "event": "chat.error",
                "payload": {"error": f"cannot connect to Jiuwen Gateway {url}: {exc}"},
            }
        )
        return 3

    cancelled = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, cancelled.set)

    try:
        raw_ack = await asyncio.wait_for(ws.recv(), timeout=10.0)
        ack = json.loads(raw_ack)
        if ack.get("type") != "event" or ack.get("event") != "connection.ack":
            raise RuntimeError(f"expected connection.ack, got {ack!r}")

        # This synthetic public event lets OpenOPC persist the provider token
        # before the first model/tool event, including across process crashes.
        _emit(
            {
                "type": "event",
                "event": "opc.jiuwen.session",
                "payload": {
                    "session_id": args.session,
                    "mode": args.mode,
                    "execution_unit_kind": (
                        "opaque_external_team" if _is_team_mode(args.mode) else "external_agent"
                    ),
                },
            }
        )
        params: dict[str, Any] = {
            "session_id": args.session,
            "content": prompt,
            "query": prompt,
            "mode": args.mode,
            "cwd": str(Path(args.cwd).expanduser().resolve()),
            "project_dir": str(Path(args.project_dir).expanduser().resolve()),
            "trusted_dirs": [
                str(Path(item).expanduser().resolve()) for item in list(args.trusted_dir or [])
            ],
            "supports_user_interaction": True,
            "agent_ref": {"mode": args.mode, "id": "default"},
        }
        if args.agent_group_name:
            params["agent_group_name"] = args.agent_group_name
        await _send(
            ws,
            {
                "type": "req",
                "id": f"chat-{uuid.uuid4().hex[:12]}",
                "method": "chat.send",
                "is_stream": True,
                "params": params,
            },
        )

        team_mode = _is_team_mode(args.mode)
        deadline = loop.time() + float(args.timeout) if args.timeout and args.timeout > 0 else 0.0
        team_final_deadline = 0.0
        awaiting_resume = False
        while True:
            if deadline and loop.time() >= deadline:
                await _interrupt(ws, args.session)
                _emit({"type": "event", "event": "chat.error", "payload": {"error": "Jiuwen response timeout"}})
                return 124
            if team_final_deadline and loop.time() >= team_final_deadline:
                # Jiuwen currently emits no processing_status(False) when a
                # Team leader answers without creating internal tasks.
                return 0
            recv_task = asyncio.create_task(ws.recv())
            cancel_task = asyncio.create_task(cancelled.wait())
            wait_timeout: float | None = None
            if deadline:
                wait_timeout = min(1.0, max(0.0, deadline - loop.time()))
            if team_final_deadline:
                team_idle_remaining = max(0.0, team_final_deadline - loop.time())
                wait_timeout = min(wait_timeout, team_idle_remaining) if wait_timeout is not None else team_idle_remaining
            done, pending = await asyncio.wait(
                {recv_task, cancel_task},
                timeout=wait_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for pending_task in pending:
                pending_task.cancel()
            if not done:
                continue
            if cancel_task in done and bool(cancel_task.result()):
                recv_task.cancel()
                await _interrupt(ws, args.session)
                return 130
            cancel_task.cancel()
            try:
                raw = recv_task.result()
            except Exception as exc:
                _emit({"type": "event", "event": "chat.error", "payload": {"error": f"Jiuwen Gateway connection lost: {exc}"}})
                return 3
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(frame, dict) or frame.get("type") != "event":
                continue
            _emit(frame)
            event_type = str(frame.get("event") or "")
            payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
            if team_final_deadline and event_type != "chat.final":
                team_final_deadline = 0.0
            if event_type in {"chat.ask_user_question", "plan.approval_required"}:
                awaiting_resume = bool(
                    str(payload.get("source") or "") in _INTERRUPT_RESUME_SOURCES
                    and str(payload.get("request_id") or "").strip()
                )
                if not await _answer_interaction(
                    ws,
                    session_id=args.session,
                    mode=args.mode,
                    payload=payload,
                ):
                    await _interrupt(ws, args.session)
                    return 130
                continue
            terminal, exit_code = _is_terminal(event_type, payload, team_mode=team_mode)
            if terminal:
                if awaiting_resume:
                    awaiting_resume = False
                    continue
                return exit_code
            if (
                team_mode
                and event_type == "chat.final"
                and str(payload.get("event_type") or "") in {"", "chat.final"}
            ):
                team_final_deadline = loop.time() + 3.0
    except Exception as exc:
        _emit({"type": "event", "event": "chat.error", "payload": {"error": str(exc)}})
        return 1
    finally:
        with contextlib.suppress(Exception):
            await ws.close()


def main() -> None:
    args = _parser().parse_args()
    try:
        raise SystemExit(asyncio.run(_main(args)))
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()

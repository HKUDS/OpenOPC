"""dsh external-agent adapter: ACP wrapper protocol tests.

These tests exercise the full line-protocol contract without a real dsh
installation or API key: a fake ``dsh --profile acp`` server drives the
wrapper through session handshake, semantic progress, the approval bridge,
and clean exit. The adapter-side parse/format helpers are tested directly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import pytest

from opc.core.config import AgentsConfig
from opc.layer3_agent.adapters.dsh_adapter import DshAdapter

FAKE_DSH_SERVER = r'''#!/usr/bin/env python3
import json, sys

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

def read():
    line = sys.stdin.readline()
    return json.loads(line) if line.strip() else None

while True:
    msg = read()
    if msg is None:
        break
    mid, method = msg.get("id"), msg.get("method")
    if method == "session/new":
        send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "sess-123"}})
    elif method == "session/prompt":
        send({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": "sess-123",
            "update": {"content": [{"type": "text", "text": "Starting the work..."}]}}})
        send({"jsonrpc": "2.0", "id": 100, "method": "session/request_permission", "params": {
            "sessionId": "sess-123",
            "toolCall": {"toolCallId": "call-1"},
            "options": [{"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                        {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"}]}})
        resp = read()
        assert resp.get("id") == 100, resp
        send({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": "sess-123",
            "update": {"content": [{"type": "text", "text": "Permission granted, continuing..."}]}}})
        send({"jsonrpc": "2.0", "id": mid, "result": {"ok": True}})
    elif method == "session/resume":
        assert "cwd" in msg["params"], "resume must carry cwd"
        # Real dsh returns an empty result on resume; the session keeps the
        # requested id, so the wrapper must not demand a sessionId back.
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    else:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "unknown"}})
'''


@pytest.fixture()
def fake_dsh_server(tmp_path):
    path = tmp_path / "fake_dsh_server.py"
    path.write_text(FAKE_DSH_SERVER, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _wait_for_line(proc, want_type, timeout=10):
    deadline = __import__("time").time() + timeout
    seen = []
    while __import__("time").time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            seen.append(line.strip())
            continue
        seen.append(obj)
        if isinstance(obj, dict) and obj.get("type") == want_type:
            return obj, seen
    raise AssertionError(
        f"did not see {want_type} in {timeout}s; saw: {seen}; "
        f"stderr={proc.stderr.read() if proc.poll() is not None else '<still running>'}"
    )


def _run_wrapper(fake_dsh_server, cwd, *extra, env=None):
    merged = dict(os.environ)
    merged["DSH_CMD"] = fake_dsh_server
    if env:
        merged.update(env)
    return subprocess.Popen(
        [sys.executable, "-m", "opc.layer3_agent.adapters.dsh_acp_wrapper",
         "--cwd", cwd, *extra, "--", "Do the thing"],
        cwd=os.getcwd(),
        env=merged,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_wrapper_handshake_progress_approval_and_exit(fake_dsh_server, tmp_path):
    """The wrapper bridges session id, progress, approval, and exits cleanly."""
    proc = _run_wrapper(fake_dsh_server, str(tmp_path))
    sess, _ = _wait_for_line(proc, "session")
    assert sess["id"] == "sess-123"
    prog, _ = _wait_for_line(proc, "progress")
    assert "Starting" in prog["text"]
    approval, _ = _wait_for_line(proc, "approval")
    assert approval["callId"] == 100
    assert proc.stdin is not None
    proc.stdin.write(json.dumps({"type": "approval_response", "callId": 100, "allowed": True}) + "\n")
    proc.stdin.flush()
    prog2, _ = _wait_for_line(proc, "progress")
    assert "continuing" in prog2["text"]
    rc = proc.wait(timeout=10)
    assert rc == 0, f"wrapper exit {rc}; stderr={proc.stderr.read()}"


def test_wrapper_resume_handshake(fake_dsh_server, tmp_path):
    """--resume routes through session/resume and keeps the provided id."""
    proc = _run_wrapper(fake_dsh_server, str(tmp_path), "--resume", "sess-old")
    sess, _ = _wait_for_line(proc, "session")
    assert sess["id"] == "sess-old"
    proc.kill()


def test_wrapper_rejects_when_approval_mode_is_restrictive(fake_dsh_server, tmp_path):
    """--approval reject decides locally instead of waiting on the bridge."""
    proc = _run_wrapper(fake_dsh_server, str(tmp_path), "--approval", "reject")
    _wait_for_line(proc, "session")
    _wait_for_line(proc, "approval")
    # No approval response is written; the wrapper must still finish.
    rc = proc.wait(timeout=10)
    assert rc == 0, f"wrapper exit {rc}; stderr={proc.stderr.read()}"


def test_adapter_line_bridge_roundtrip():
    """parse/format/progress/resume helpers speak the wrapper's line protocol."""
    cfg = AgentsConfig()
    adapter = DshAdapter(config=cfg.agents["dsh"])
    assert adapter._transport() == "acp"
    assert adapter.supports_approval_prompt_handling([], {})
    assert adapter.supports_session_resume()

    request = adapter.parse_approval_request(
        json.dumps({"type": "approval", "callId": 100, "toolName": "bash",
                    "arguments": {"cmd": "ls"}, "promptText": "run?"})
    )
    assert request is not None
    assert request.approval_scope == "tool"
    assert request.action_name == "bash"
    assert request.metadata["call_id"] == "100"
    response = adapter.format_approval_response(request, True, None)
    assert json.loads(response) == {"type": "approval_response", "callId": "100", "allowed": True}

    assert adapter.format_progress_update('{"type":"progress","text":"hi"}') == "hi"
    assert adapter.format_progress_update("plain text") is None

    raw = (
        '{"type": "session", "id": "s-1"}\n'
        '{"type": "progress", "text": "Thinking..."}\n'
        '{"type": "approval", "callId": 1, "toolName": "bash", "arguments": {}}\n'
        '{"type": "progress", "text": "FORMAT-CHECK"}\n'
        "plain fallback line\n"
    )
    normalized = adapter.normalize_result_output(raw)
    assert "session" not in normalized and "approval" not in normalized
    assert "Thinking..." in normalized and "FORMAT-CHECK" in normalized
    assert normalized.count("plain fallback line") == 1
    assert adapter.extract_resume_session_id('{"type":"session","id":"sess-9"}') == "sess-9"


def test_build_task_prompt_adds_resume_guidance_for_same_session():
    """Resumed conversations tell dsh the brief is a follow-up, not a new task."""
    cfg = AgentsConfig()
    adapter = DshAdapter(config=cfg.agents["dsh"])

    def make_task(snapshot):
        return type("T", (), {
            "title": "Latest message",
            "description": "Latest message",
            "metadata": {},
            "context_snapshot": snapshot,
        })()

    plain = make_task(None)
    assert adapter.build_task_prompt(plain) == "Latest message"

    resumed = make_task({"runtime_resume": {"restored_from_same_session": True}})
    prompt = adapter.build_task_prompt(resumed)
    assert "continuing an ongoing OpenOPC conversation" in prompt
    assert prompt.endswith("Latest message")

    not_resumed = make_task({"runtime_resume": {"restored_from_same_session": False}})
    assert adapter.build_task_prompt(not_resumed) == "Latest message"


def test_adapter_build_invocation_uses_wrapper_by_default():
    """Default transport composes the wrapper command with cwd and prompt."""
    cfg = AgentsConfig()
    adapter = DshAdapter(config=cfg.agents["dsh"])
    task = type("T", (), {"title": "Title", "description": "Desc", "metadata": {}})()
    cmd, metadata = adapter.build_invocation(task, "/tmp/ws")
    assert any("dsh_acp_wrapper" in part for part in cmd)
    assert "--cwd" in cmd and cmd[cmd.index("--cwd") + 1].endswith("/ws")
    assert cmd[-2:] == ["--", "Title\n\nDesc"]
    assert metadata["transport"] == "acp"


def test_dshswarm_team_envelope_parsing():
    """dshswarm projects the team envelope into structured result fields."""
    from opc.layer3_agent.adapters.dsh_adapter import DshSwarmAdapter
    cfg = AgentsConfig()
    adapter = DshSwarmAdapter(config=cfg.agents["dshswarm"])
    assert adapter.execution_unit_kind() == "opaque_external_team"
    assert adapter.supports_company_execution() is True

    output = (
        "some prose before\n"
        + json.dumps({
            "work_item_id": "wi-1",
            "attempt_id": "a-7",
            "status": "done",
            "summary": "团队完成了任务",
            "deliverables": [
                {"kind": "file", "name": "report.md", "path": "/ws/report.md", "description": "报告"},
            ],
            "verification": {"verdict": "pass", "command": "pytest"},
            "risks": ["风险"],
            "open_questions": [],
            "handoff": "请查看报告",
        })
        + "\nmore prose"
    )
    structured = adapter.extract_structured_result_fields(output)
    assert structured["opaque_external_team_result"]["work_item_id"] == "wi-1"
    assert structured["opaque_external_team_result"]["status"] == "done"
    assert structured["work_item_artifact_index"][0]["value"] == "/ws/report.md"
    assert structured["verification_evidence"]["verdict"] == "pass"

    # company-mode validation passes for a complete envelope
    task = type("T", (), {
        "metadata": {"execution_mode": "company_mode", "execution_unit_kind": "opaque_external_team", "attempt_id": "a-7"},
        "title": "", "description": "", "context_snapshot": {},
    })()
    assert adapter.validate_result_output(output, task) is None

    # mismatch on work_item_id is rejected
    task2 = type("T", (), {
        "metadata": {"execution_mode": "company_mode", "execution_unit_kind": "opaque_external_team", "attempt_id": "a-7"},
        "title": "", "description": "",
        "context_snapshot": {},
    })()
    import opc.layer3_agent.adapters.dsh_adapter as dsh_mod
    from opc.layer2_organization.work_item_links import linked_work_item_id_for_task
    # simulate linked work item by patching the helper
    orig = linked_work_item_id_for_task
    try:
        dsh_mod.linked_work_item_id_for_task = lambda t: "wi-expected"
        err = adapter.validate_result_output(output, task2)
        assert err and "work_item_id mismatch" in err
    finally:
        dsh_mod.linked_work_item_id_for_task = orig


def test_dshswarm_build_invocation_uses_team_profile():
    """dshswarm composes the wrapper invocation with the team profile."""
    from opc.layer3_agent.adapters.dsh_adapter import DshSwarmAdapter
    cfg = AgentsConfig()
    adapter = DshSwarmAdapter(config=cfg.agents["dshswarm"])
    task = type("T", (), {"title": "T", "description": "D", "metadata": {}, "context_snapshot": {}})()
    cmd, metadata = adapter.build_invocation(task, "/tmp/ws")
    assert any("dsh_acp_wrapper" in part for part in cmd)
    assert metadata["profile"] == "team"
    assert "--cwd" in cmd and cmd[cmd.index("--cwd") + 1].endswith("/ws")


def test_dsh_supports_company_execution():
    """Company fence capability is on for the single-agent dsh adapter."""
    cfg = AgentsConfig()
    from opc.layer3_agent.adapters.dsh_adapter import DshAdapter
    adapter = DshAdapter(config=cfg.agents["dsh"])
    assert adapter.supports_company_execution() is True

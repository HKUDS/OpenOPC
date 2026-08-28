from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from opc.core.config import AutonomyConfig
from opc.core.models import PermissionResolution, Task
from opc.core.native_permissions import (
    NativePermissionPolicyResolver,
    migrate_legacy_native_approval_level,
    native_sandbox_profile,
    parse_native_approval_level,
    tasks_in_native_permission_scope,
)
from opc.layer2_organization.approval import ApprovalEngine
from opc.layer4_tools.execution_context import wrap_command_for_context


def _tool(name: str, *effects: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, permission_effects=effects)


def _task(scope: str, level: str, workspace: Path) -> Task:
    return Task(
        id=f"task-{scope}",
        project_id="permission-tests",
        session_id=scope,
        metadata={
            "native_approval_level": level,
            "native_permission_scope_id": scope,
            "workspace_root": str(workspace),
        },
    )


@pytest.mark.parametrize(
    ("legacy", "expected"),
    [
        ({"native_approval_level": "read-only"}, "read-only"),
        ({"enabled": False}, "full-access"),
        ({"allow_native_tool_auto_approval": False}, "read-only"),
        (
            {
                "max_auto_approve_risk": "high",
                "approval_confidence_threshold": 0.0,
                "tool_first_use_approval": False,
            },
            "full-access",
        ),
        ({"enabled": True}, "auto"),
    ],
)
def test_legacy_native_permission_migration(legacy: dict, expected: str) -> None:
    assert migrate_legacy_native_approval_level(legacy) == expected
    assert AutonomyConfig.model_validate(legacy).native_approval_level == expected


def test_invalid_explicit_native_permission_level_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_native_approval_level("maybe")
    with pytest.raises(ValueError):
        AutonomyConfig.model_validate({"native_approval_level": "maybe"})


def test_company_descendants_resolve_to_root_permission_scope() -> None:
    root = Task(id="root", session_id="root-session")
    child = Task(
        id="child",
        session_id="child-session",
        parent_session_id="root-session",
    )
    grandchild = Task(
        id="grandchild",
        session_id="grandchild-session",
        parent_session_id="child-session",
    )
    unrelated = Task(id="other", session_id="other-session")
    resolved = tasks_in_native_permission_scope(
        [grandchild, unrelated, child, root],
        root_session_id="root-session",
        scope_id="root-session",
    )
    assert {task.id for task in resolved} == {"root", "child", "grandchild"}


@pytest.mark.parametrize(
    ("level", "tool", "kwargs", "expected"),
    [
        ("read-only", _tool("file_read", "local_read"), {}, PermissionResolution.ALLOW),
        ("read-only", _tool("file_write", "workspace_write"), {}, PermissionResolution.ASK),
        ("read-only", _tool("shell_exec", "process_execute", "workspace_write"), {"safe_read_only_process": True}, PermissionResolution.ALLOW),
        ("read-only", _tool("shell_exec", "process_execute", "workspace_write"), {}, PermissionResolution.ASK),
        ("read-only", _tool("web_search", "local_read", "network_access"), {}, PermissionResolution.ASK),
        ("auto", _tool("file_write", "workspace_write"), {}, PermissionResolution.ALLOW),
        ("auto", _tool("shell_exec", "process_execute", "workspace_write"), {}, PermissionResolution.ALLOW),
        ("auto", _tool("web_search", "local_read", "network_access"), {}, PermissionResolution.ASK),
        ("auto", _tool("dynamic"), {}, PermissionResolution.ASK),
        ("full-access", _tool("dynamic"), {}, PermissionResolution.ALLOW),
        ("full-access", _tool("browser", "external_side_effect"), {}, PermissionResolution.ALLOW),
    ],
)
def test_native_permission_decision_matrix(
    tmp_path: Path,
    level: str,
    tool: SimpleNamespace,
    kwargs: dict,
    expected: PermissionResolution,
) -> None:
    resolver = NativePermissionPolicyResolver(
        AutonomyConfig(native_approval_level=level)
    )
    with patch.object(resolver, "_sandbox_available", return_value=True):
        decision = resolver.evaluate(
            tool,
            task=_task(level, level, tmp_path),
            **kwargs,
        )
    assert decision.resolution == expected


def test_sandbox_unavailable_never_silently_falls_back(tmp_path: Path) -> None:
    resolver = NativePermissionPolicyResolver(
        AutonomyConfig(native_approval_level="auto")
    )
    with patch.object(resolver, "_sandbox_available", return_value=False):
        decision = resolver.evaluate(
            _tool("shell_exec", "process_execute", "workspace_write"),
            task=_task("scope", "auto", tmp_path),
        )
    assert decision.resolution == PermissionResolution.ASK
    assert decision.metadata["sandbox_override"]["mode"] == "off"
    assert decision.metadata["sandbox_override"]["grant_scope"] == "once"


def test_session_scopes_are_isolated_and_switch_live(tmp_path: Path) -> None:
    resolver = NativePermissionPolicyResolver(
        AutonomyConfig(native_approval_level="auto")
    )
    first = _task("session-a", "read-only", tmp_path)
    second = _task("session-b", "full-access", tmp_path)
    assert resolver.context_for_task(first).level == "read-only"
    assert resolver.context_for_task(second).level == "full-access"

    resolver.set_session_level("session-a", "auto")
    assert resolver.context_for_task(first).level == "auto"
    assert resolver.context_for_task(second).level == "full-access"


def test_native_sandbox_profiles() -> None:
    assert native_sandbox_profile("read-only")["mode"] == "read-only"
    assert native_sandbox_profile("read-only")["allow_network"] is False
    assert native_sandbox_profile("auto")["mode"] == "workspace-write"
    assert native_sandbox_profile("full-access")["enabled"] is False
    assert native_sandbox_profile("full-access")["mode"] == "off"


def test_bwrap_read_only_and_workspace_write_profiles(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    base = {"workspace_root": str(workspace)}
    with patch("opc.layer4_tools.execution_context.shutil.which", return_value="/usr/bin/bwrap"):
        read_args, _ = wrap_command_for_context(
            ["/usr/bin/true"],
            cwd=str(workspace),
            context={**base, "sandbox": {**native_sandbox_profile("read-only"), "platform": "linux"}},
        )
        auto_args, _ = wrap_command_for_context(
            ["/usr/bin/true"],
            cwd=str(workspace),
            context={**base, "sandbox": {**native_sandbox_profile("auto"), "platform": "linux"}},
        )
    workspace_bind = ["--bind", str(workspace), str(workspace)]
    assert not any(
        read_args[index:index + 3] == workspace_bind
        for index in range(len(read_args) - 2)
    )
    assert ["--bind", str(workspace), str(workspace)] == auto_args[auto_args.index("--bind"):auto_args.index("--bind") + 3]
    assert "--unshare-net" in read_args
    assert "--unshare-net" in auto_args


def test_macos_read_only_and_workspace_write_profiles(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    base = {"workspace_root": str(workspace)}
    with patch(
        "opc.layer4_tools.execution_context.shutil.which",
        return_value="/usr/bin/sandbox-exec",
    ):
        read_args, _ = wrap_command_for_context(
            ["/usr/bin/true"],
            cwd=str(workspace),
            context={
                **base,
                "sandbox": {
                    **native_sandbox_profile("read-only"),
                    "platform": "macos",
                },
            },
        )
        auto_args, _ = wrap_command_for_context(
            ["/usr/bin/true"],
            cwd=str(workspace),
            context={
                **base,
                "sandbox": {
                    **native_sandbox_profile("auto"),
                    "platform": "macos",
                },
            },
        )
    read_profile = read_args[read_args.index("-p") + 1]
    auto_profile = auto_args[auto_args.index("-p") + 1]
    assert f'(subpath "{workspace}")' not in read_profile
    assert f'(subpath "{workspace}")' in auto_profile
    assert "(allow network*)" not in read_profile
    assert "(allow network*)" not in auto_profile


def test_required_macos_sandbox_never_falls_back_when_unavailable(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with patch("opc.layer4_tools.execution_context.shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="direct fallback is disabled"):
            wrap_command_for_context(
                ["/usr/bin/true"],
                cwd=str(workspace),
                context={
                    "workspace_root": str(workspace),
                    "sandbox": {
                        **native_sandbox_profile("auto"),
                        "platform": "macos",
                    },
                },
            )


class _Preferences:
    def get_autonomy_preferences(self, project_id=None):
        return {"learned_actions": {}}


class _Store:
    async def record_approval(self, **kwargs):
        return None


class _Memory:
    def append_autonomy_event(self, event, project=False):
        return None


def test_auto_compound_workspace_shell_does_not_ask_only_for_syntax(tmp_path: Path) -> None:
    config = AutonomyConfig(native_approval_level="auto")
    engine = ApprovalEngine(
        llm=object(),
        store=_Store(),
        preferences=_Preferences(),
        memory=_Memory(),
        config=config,
    )
    shell = _tool("shell_exec", "process_execute", "workspace_write")
    task = _task("compound", "auto", tmp_path)
    with patch.object(engine.native_policy, "_sandbox_available", return_value=True):
        decision = engine.predict(
            shell,
            {"command": "mkdir -p reports && printf done > reports/status.txt"},
            task=task,
        )
    assert decision.resolution == PermissionResolution.ALLOW


def test_auto_shell_literal_outside_workspace_asks_with_minimal_path_grant(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    engine = ApprovalEngine(
        llm=object(),
        store=_Store(),
        preferences=_Preferences(),
        memory=_Memory(),
        config=AutonomyConfig(native_approval_level="auto"),
    )
    with patch.object(engine.native_policy, "_sandbox_available", return_value=True):
        decision = engine.predict(
            _tool("shell_exec", "process_execute", "workspace_write"),
            {
                "command": f"printf done > {outside / 'status.txt'}",
                "working_directory": str(workspace),
            },
            task=_task("outside-shell", "auto", workspace),
        )
    assert decision.resolution == PermissionResolution.ASK
    override = decision.metadata["sandbox_override"]
    assert override["mode"] == "workspace-write"
    assert override["additional_write_paths"] == [str(outside)]


def test_auto_python_network_code_asks_with_network_only_elevation(
    tmp_path: Path,
) -> None:
    engine = ApprovalEngine(
        llm=object(),
        store=_Store(),
        preferences=_Preferences(),
        memory=_Memory(),
        config=AutonomyConfig(native_approval_level="auto"),
    )
    with patch.object(engine.native_policy, "_sandbox_available", return_value=True):
        decision = engine.predict(
            _tool("python_exec", "process_execute", "workspace_write"),
            {"code": "import requests\nprint(requests.get('https://example.com').status_code)"},
            task=_task("python-network", "auto", tmp_path),
        )
    assert decision.resolution == PermissionResolution.ASK
    override = decision.metadata["sandbox_override"]
    assert override["mode"] == "workspace-write"
    assert override["allow_network"] is True
    assert "additional_write_paths" not in override


def test_explicit_deny_still_wins_in_full_access(tmp_path: Path) -> None:
    config = AutonomyConfig(
        native_approval_level="full-access",
        permissions_v2={"deny_tools": ["shell_exec"]},
    )
    engine = ApprovalEngine(
        llm=object(), store=_Store(), preferences=_Preferences(), memory=_Memory(), config=config,
    )
    decision = engine.predict(
        _tool("shell_exec", "process_execute", "workspace_write"),
        {"command": "pwd"},
        task=_task("full", "full-access", tmp_path),
    )
    assert decision.resolution == PermissionResolution.DENY

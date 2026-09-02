"""Preflight checks for OpenOPC external-agent integration."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opc.core.config import OPCConfig
from opc.core.models import Task
from opc.layer3_agent.adapters.base import ExternalAgentAdapter
from opc.layer3_agent.adapters.registry import ADAPTER_CLASSES
from opc.layer3_agent.jiuwen_gateway import (
    probe_jiuwen_gateway,
    resolve_jiuwen_gateway_url,
)
from opc.layer3_agent.skill_installer import (
    install_collab_surface,
    opc_collab_executable,
)


_HELP_COMMANDS: dict[str, tuple[str, ...]] = {
    "codex": ("exec", "--help"),
    "claude_code": ("--help",),
    "cursor": ("--help",),
    "opencode": ("run", "--help"),
    "jiuwen": ("chat", "--help"),
    "jiuwenswarm": ("chat", "--help"),
}


_VERSION_COMMANDS: dict[str, tuple[str, ...]] = {
    "codex": ("--version",),
    "claude_code": ("--version",),
    "cursor": ("--version",),
    "opencode": ("--version",),
    "jiuwen": ("--version",),
    "jiuwenswarm": ("--version",),
}


@dataclass
class PathProbeResult:
    name: str
    path: str
    ok: bool
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "ok": self.ok,
            "error": self.error,
        }


@dataclass
class ExternalAgentPreflightResult:
    agent: str
    enabled: bool
    command: str
    available: bool
    binary: str = ""
    version: str = ""
    launch_command: str = ""
    stdin_policy: str = ""
    collaboration_rpc_transport: str = ""
    isolated_home: str = ""
    collab_cli: str = ""
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    write_checks: list[PathProbeResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.enabled and self.available and not self.issues)

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "enabled": self.enabled,
            "command": self.command,
            "available": self.available,
            "binary": self.binary,
            "version": self.version,
            "launch_command": self.launch_command,
            "stdin_policy": self.stdin_policy,
            "collaboration_rpc_transport": self.collaboration_rpc_transport,
            "isolated_home": self.isolated_home,
            "collab_cli": self.collab_cli,
            "issues": list(self.issues),
            "warnings": list(self.warnings),
            "write_checks": [check.as_dict() for check in self.write_checks],
            "ok": self.ok,
        }


class ExternalAgentPreflightError(RuntimeError):
    """Raised when the workspace permission contract is not writable."""

    def __init__(self, checks: list[PathProbeResult]) -> None:
        self.checks = checks
        failures = [check for check in checks if not check.ok]
        detail = "; ".join(f"{item.name}={item.path}: {item.error}" for item in failures)
        super().__init__(f"External agent workspace permission preflight failed: {detail}")


def ensure_external_agent_surfaces(
    config: OPCConfig,
    *,
    opc_home: Path | None = None,
) -> dict[str, dict[str, str]]:
    """Provision isolated agent homes and the shared opc-collab shim.

    This is intentionally safe to run from ``opc init`` and ``opc status``:
    surfaces are idempotent and user config is mirrored into OpenOPC-owned
    isolated homes instead of mutating the user's real agent configuration.
    """
    base_home = Path(opc_home) if opc_home else _get_opc_home()
    surfaces: dict[str, dict[str, str]] = {}
    for agent_name, adapter_cls in ADAPTER_CLASSES.items():
        agent_config = config.agents.agents.get(agent_name)
        adapter = adapter_cls(config=agent_config)
        if agent_config and not agent_config.enabled:
            continue
        slug = adapter.agent_isolation_home_slug()
        if not slug:
            continue
        home, bin_dir = install_collab_surface(slug, opc_home=base_home)
        adapter.post_install_agent_home(str(home))
        surfaces[agent_name] = {
            "home": str(home),
            "bin_dir": str(bin_dir),
            "collab_cli": str(opc_collab_executable(bin_dir)),
        }
    return surfaces


def probe_external_agent_write_contract(
    *,
    workspace_path: str | Path,
    opc_home: Path | None = None,
    task: Task | None = None,
    project_db_path: str | Path | None = None,
) -> list[PathProbeResult]:
    """Check every path external agents must be able to write before launch."""
    base_home = Path(opc_home) if opc_home else _get_opc_home()
    metadata = dict(getattr(task, "metadata", {}) or {})
    probes: list[tuple[str, Path, bool]] = [
        ("workspace", Path(workspace_path).expanduser(), False),
        ("opc_home", base_home.expanduser(), False),
        ("opc_memory", (base_home / "memory").expanduser(), False),
    ]

    for name, key in (
        ("collab_workspace", "comms_workspace_root"),
        ("collab_root", "comms_root"),
        ("output_root", "output_root"),
        ("target_output_dir", "target_output_dir"),
    ):
        raw = str(metadata.get(key) or "").strip()
        if raw:
            probes.append((name, Path(raw).expanduser(), False))

    if project_db_path:
        probes.append(("project_db", Path(project_db_path).expanduser(), True))

    results: list[PathProbeResult] = []
    seen: set[tuple[str, str]] = set()
    for name, path, is_file in probes:
        key = (name, str(path))
        if key in seen:
            continue
        seen.add(key)
        results.append(_probe_writable_path(name, path, is_file=is_file))
    return results


def assert_external_agent_write_contract(
    *,
    workspace_path: str | Path,
    opc_home: Path | None = None,
    task: Task | None = None,
    project_db_path: str | Path | None = None,
) -> list[PathProbeResult]:
    checks = probe_external_agent_write_contract(
        workspace_path=workspace_path,
        opc_home=opc_home,
        task=task,
        project_db_path=project_db_path,
    )
    if any(not check.ok for check in checks):
        raise ExternalAgentPreflightError(checks)
    return checks


def run_external_agent_preflight(
    config: OPCConfig,
    *,
    project_id: str = "default",
    workspace_path: str | Path | None = None,
    opc_home: Path | None = None,
    probe_commands: bool = True,
    prepare_surfaces: bool = True,
) -> list[ExternalAgentPreflightResult]:
    base_home = Path(opc_home) if opc_home else _get_opc_home()
    if workspace_path is None:
        workspace = _get_project_workplace(project_id)
    else:
        workspace = Path(workspace_path)
    workspace.mkdir(parents=True, exist_ok=True)

    surfaces = (
        ensure_external_agent_surfaces(config, opc_home=base_home)
        if prepare_surfaces
        else {}
    )
    sample_task = _sample_preflight_task(project_id, workspace)
    project_db = base_home / "projects" / project_id / "tasks.db"
    write_checks = probe_external_agent_write_contract(
        workspace_path=workspace,
        opc_home=base_home,
        task=sample_task,
        project_db_path=project_db,
    )
    rpc_transport, rpc_issue = _describe_collaboration_rpc_transport()

    results: list[ExternalAgentPreflightResult] = []
    for agent_name, adapter_cls in ADAPTER_CLASSES.items():
        agent_config = config.agents.agents.get(agent_name)
        adapter = adapter_cls(config=agent_config)
        command = adapter.configured_command()
        enabled = bool(adapter.config.enabled)
        binary = adapter.resolve_binary() if enabled else None
        result = ExternalAgentPreflightResult(
            agent=agent_name,
            enabled=enabled,
            command=command,
            available=bool(binary),
            binary=str(binary or ""),
            write_checks=list(write_checks),
            collaboration_rpc_transport=rpc_transport,
        )
        surface = surfaces.get(agent_name, {})
        result.isolated_home = surface.get("home", "")
        result.collab_cli = surface.get("collab_cli", "")

        if not enabled:
            result.warnings.append("disabled in config")
            results.append(result)
            continue

        cmd: list[str] = []
        try:
            cmd, metadata = _build_sample_invocation(adapter, sample_task, str(workspace))
            result.launch_command = str(
                metadata.get("display_command") or metadata.get("command") or shlex.join(cmd)
            )
            result.stdin_policy = _describe_stdin_policy(adapter, cmd, metadata)
        except Exception as exc:
            result.warnings.append(f"sample invocation unavailable: {exc}")

        if not binary:
            result.issues.append(_missing_agent_issue(agent_name, command))
            if rpc_issue:
                result.issues.append(rpc_issue)
            results.append(result)
            continue

        _add_windows_wrapper_warnings(command, str(binary), result.warnings)
        if rpc_issue:
            result.issues.append(rpc_issue)
        if probe_commands:
            if (
                agent_name in {"jiuwen", "jiuwenswarm"}
                and str(getattr(adapter.config, "transport", "") or "").strip()
                == "gateway"
            ):
                gateway_url = resolve_jiuwen_gateway_url(
                    str(getattr(adapter.config, "gateway_url", "") or "")
                )
                gateway_issue = probe_jiuwen_gateway(gateway_url)
                if gateway_issue:
                    result.issues.append(gateway_issue)
                else:
                    result.version = f"Jiuwen Gateway protocol ({gateway_url})"
            else:
                result.version = _probe_version(agent_name, str(binary), result.warnings)
                if cmd:
                    _probe_help_flags(agent_name, str(binary), cmd, result)
        _probe_isolated_home(agent_name, result)

        for check in write_checks:
            if not check.ok:
                result.issues.append(f"{check.name} is not writable: {check.path} ({check.error})")
        results.append(result)
    return results


def _describe_collaboration_rpc_transport() -> tuple[str, str]:
    from opc.layer4_tools.collaboration_rpc import (
        OPC_COLLAB_RPC_TRANSPORT,
        resolve_collaboration_rpc_transport,
    )

    requested = str(os.environ.get(OPC_COLLAB_RPC_TRANSPORT, "")).strip().lower()
    try:
        resolved = resolve_collaboration_rpc_transport(requested or "auto")
    except Exception as exc:
        return (f"{requested or 'auto'}(unavailable)", str(exc))
    if resolved == "tcp":
        return ("tcp(loopback)", "")
    if requested and requested != resolved:
        return (f"{resolved}({requested})", "")
    return (resolved, "")


def _describe_stdin_policy(
    adapter: ExternalAgentAdapter,
    cmd: list[str],
    metadata: dict[str, Any],
) -> str:
    try:
        return adapter.stdin_policy_for_process(cmd, metadata)
    except Exception:
        explicit_policy = str(metadata.get("stdin_policy") or "").strip()
        return explicit_policy or "devnull"


def _missing_agent_issue(agent_name: str, command: str) -> str:
    if agent_name == "cursor":
        editor = shutil.which("cursor")
        agent = shutil.which("cursor-agent")
        if editor and not agent:
            return "Cursor editor found, cursor-agent missing; install Cursor Agent CLI for headless execution"
    return f"command not found on PATH: {command}"


def _add_windows_wrapper_warnings(command: str, binary: str, warnings: list[str]) -> None:
    if os.name != "nt":
        return
    path = Path(binary)
    if path.suffix.lower() == ".ps1":
        warnings.append(
            "PowerShell execution policy may block this .ps1 wrapper; use the matching .cmd command manually"
        )
        return
    if path.suffix.lower() == ".cmd":
        ps1 = path.with_suffix(".ps1")
        if ps1.exists() and Path(command).suffix == "":
            warnings.append(
                f"PowerShell may prefer {ps1.name}; use `{path.name}` manually if script execution is blocked"
            )


def _sample_preflight_task(project_id: str, workspace: Path) -> Task:
    metadata = {
        "workspace_root": str(workspace),
        "comms_workspace_root": str(workspace),
        "comms_root": str(workspace / ".opc-comms"),
        "target_output_dir": str(workspace),
    }
    return Task(
        title="OpenOPC external agent preflight",
        description="Reply with OK. This task is only used to build a launch command.",
        assigned_to="owner",
        project_id=project_id,
        metadata=metadata,
    )


def _get_opc_home() -> Path:
    from opc.core.config import get_opc_home

    return get_opc_home()


def _get_project_workplace(project_id: str) -> Path:
    from opc.core.config import get_project_workplace

    return get_project_workplace(project_id)


def _build_sample_invocation(
    adapter: ExternalAgentAdapter,
    task: Task,
    workspace: str,
) -> tuple[list[str], dict[str, Any]]:
    if adapter.config.run_mode == "interactive" and adapter.supports_interactive():
        return adapter.build_interactive_invocation(task, workspace_path=workspace)
    return adapter.build_invocation(task, workspace_path=workspace)


def _probe_writable_path(name: str, path: Path, *, is_file: bool) -> PathProbeResult:
    try:
        if is_file:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                with path.open("ab"):
                    pass
            else:
                _write_delete_probe(path.parent)
        else:
            path.mkdir(parents=True, exist_ok=True)
            _write_delete_probe(path)
        resolved = path.resolve(strict=False)
        return PathProbeResult(name=name, path=str(resolved), ok=True)
    except Exception as exc:
        return PathProbeResult(name=name, path=str(path), ok=False, error=str(exc))


def _write_delete_probe(directory: Path) -> None:
    probe_path = directory / f".opc-write-probe-{uuid.uuid4().hex}.tmp"
    probe_path.write_text("ok", encoding="utf-8")
    probe_path.unlink()


def _probe_version(agent_name: str, binary: str, warnings: list[str]) -> str:
    args = _VERSION_COMMANDS.get(agent_name)
    if not args:
        return ""
    try:
        proc = subprocess.run(
            [binary, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=2,
            check=False,
        )
    except Exception as exc:
        warnings.append(f"version probe failed: {exc}")
        return ""
    text = (proc.stdout or proc.stderr or "").strip().splitlines()
    if not text and proc.returncode != 0:
        warnings.append(f"version probe exited {proc.returncode}")
    return text[0][:200] if text else ""


def _probe_help_flags(
    agent_name: str,
    binary: str,
    launch_cmd: list[str],
    result: ExternalAgentPreflightResult,
) -> None:
    args = _HELP_COMMANDS.get(agent_name, ("--help",))
    if launch_cmd and str(launch_cmd[0] or "") != str(binary or ""):
        # launch_cmd begins with an interpreter or wrapper other than the
        # probed binary (for example ``python -m <module>``); its --help can
        # never advertise the harness flags, so skip the flag check.
        return
    try:
        proc = subprocess.run(
            [binary, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=3,
            check=False,
        )
    except Exception as exc:
        result.warnings.append(f"help probe failed: {exc}")
        return
    help_text = f"{proc.stdout}\n{proc.stderr}"
    if proc.returncode not in {0, 1, 2} and not help_text.strip():
        result.warnings.append(f"help probe exited {proc.returncode}")
        return

    for flag in _launch_flags(launch_cmd):
        if flag in {"--", "-"}:
            continue
        if flag not in help_text:
            result.warnings.append(f"launch flag not advertised by help output: {flag}")


def _launch_flags(cmd: list[str]) -> list[str]:
    flags: list[str] = []
    for item in cmd[1:]:
        value = str(item or "").strip()
        if not value.startswith("-"):
            continue
        flag = value.split("=", 1)[0]
        flags.append(flag)
    return list(dict.fromkeys(flags))


def _probe_isolated_home(agent_name: str, result: ExternalAgentPreflightResult) -> None:
    if not result.isolated_home:
        return
    home = Path(result.isolated_home)
    if not home.exists():
        result.issues.append(f"isolated agent home was not created: {home}")
    skill = home / "skills" / "opc-collab" / "SKILL.md"
    if not skill.exists():
        result.issues.append(f"opc-collab skill missing from isolated home: {skill}")
    if result.collab_cli and not Path(result.collab_cli).exists():
        result.issues.append(f"opc-collab executable missing: {result.collab_cli}")

    for source, target in _known_user_config_mirrors(agent_name, home):
        if source.exists() and not target.exists():
            result.warnings.append(f"user config exists but was not mirrored: {source}")


def _known_user_config_mirrors(agent_name: str, home: Path) -> list[tuple[Path, Path]]:
    if agent_name == "codex":
        user_home = Path.home() / ".codex"
        return [
            (user_home / "auth.json", home / "auth.json"),
            (user_home / "config.toml", home / "config.toml"),
        ]
    if agent_name == "claude_code":
        user_home = Path.home() / ".claude"
        return [
            (user_home / ".credentials.json", home / ".credentials.json"),
            (user_home / "settings.json", home / "settings.json"),
            (user_home / "settings.local.json", home / "settings.local.json"),
            (user_home / "CLAUDE.md", home / "CLAUDE.md"),
        ]
    if agent_name == "opencode":
        candidates: list[tuple[Path, Path]] = []
        raw_env = str(os.environ.get("OPENCODE_CONFIG_DIR") or "").strip()
        if raw_env:
            source_home = Path(raw_env).expanduser()
        else:
            xdg = str(os.environ.get("XDG_CONFIG_HOME") or "").strip()
            source_home = Path(xdg).expanduser() / "opencode" if xdg else Path.home() / ".config" / "opencode"
        for name in ("opencode.json", "opencode.jsonc"):
            candidates.append((source_home / name, home / name))
        return candidates
    return []

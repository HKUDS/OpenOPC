"""Execution context helpers for isolated runtime tool execution."""

from __future__ import annotations

import contextvars
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from opc.core.config import OPCConfig
from opc.core.native_permissions import native_sandbox_profile, normalize_native_approval_level


_EXECUTION_CONTEXT_OVERRIDE: contextvars.ContextVar[dict[str, Any] | None] = (
    contextvars.ContextVar("opc_tool_execution_context_override", default=None)
)


def install_execution_context_override(
    override: dict[str, Any],
) -> contextvars.Token:
    """Install a coroutine-local override for one tool invocation."""

    return _EXECUTION_CONTEXT_OVERRIDE.set(dict(override or {}))


def reset_execution_context_override(token: contextvars.Token) -> None:
    _EXECUTION_CONTEXT_OVERRIDE.reset(token)


def platform_key() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def venv_python_path(venv_path: str | Path) -> Path:
    base = Path(venv_path)
    if platform_key() == "windows":
        return base / "Scripts" / "python.exe"
    return base / "bin" / "python"


def build_task_execution_context(
    *,
    workspace_root: str | Path | None,
    output_root: str | Path | None = None,
    comms_root: str | Path | None = None,
    config: OPCConfig | None = None,
    venv_path: str | Path | None = None,
    python_executable: str | Path | None = None,
    venv_provider: str = "",
    preparation_error: str = "",
    native_approval_level: str | None = None,
) -> dict[str, Any]:
    workspace = Path(workspace_root or os.getcwd()).resolve()
    output = Path(output_root).resolve() if output_root else workspace
    resolved_comms_root = Path(comms_root).resolve() if comms_root else (workspace / ".opc-comms")
    resolved_venv = Path(venv_path).resolve() if venv_path else None
    resolved_python = Path(python_executable).resolve() if python_executable else None
    return {
        "workspace_root": str(workspace),
        "output_root": str(output),
        "comms_root": str(resolved_comms_root),
        "venv_path": str(resolved_venv) if resolved_venv else "",
        "python_executable": str(resolved_python) if resolved_python else "",
        "venv_provider": str(venv_provider or "").strip(),
        "preparation_error": str(preparation_error or "").strip(),
        "sandbox": resolve_sandbox_config(config, native_approval_level=native_approval_level),
    }


def ensure_task_execution_context(task: Any, config: OPCConfig | None = None) -> dict[str, Any]:
    metadata = getattr(task, "metadata", {}) or {}
    existing = dict(metadata.get("_execution_context", {}) or {})
    if existing:
        level = metadata.get("native_approval_level")
        if level not in (None, ""):
            existing["sandbox"] = resolve_sandbox_config(
                config, native_approval_level=str(level),
            )
            metadata["_execution_context"] = existing
            setattr(task, "metadata", metadata)
        return existing
    workspace_root = (
        str(metadata.get("workspace_root", "") or "").strip()
        or str(metadata.get("comms_workspace_root", "") or "").strip()
        or str(metadata.get("target_output_dir", "") or "").strip()
        or os.getcwd()
    )
    output_root = (
        str(metadata.get("output_root", "") or "").strip()
        or str(metadata.get("target_output_dir", "") or "").strip()
        or workspace_root
    )
    comms_root = (
        str(metadata.get("comms_root", "") or "").strip()
        or (str(Path(workspace_root).resolve() / ".opc-comms") if workspace_root else "")
    )
    context = build_task_execution_context(
        workspace_root=workspace_root,
        output_root=output_root,
        comms_root=comms_root,
        config=config,
        native_approval_level=str(metadata.get("native_approval_level", "") or "") or None,
    )
    metadata["_execution_context"] = context
    setattr(task, "metadata", metadata)
    return context


def resolve_task_execution_context(
    task: Any = None,
    *,
    override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if task is None:
        return {}
    metadata = getattr(task, "metadata", {}) or {}
    context = dict(metadata.get("_execution_context", {}) or {})
    if not context:
        workspace_root = (
            str(metadata.get("workspace_root", "") or "").strip()
            or str(metadata.get("comms_workspace_root", "") or "").strip()
            or str(metadata.get("target_output_dir", "") or "").strip()
        )
        if workspace_root:
            output_root = (
                str(metadata.get("output_root", "") or "").strip()
                or str(metadata.get("target_output_dir", "") or "").strip()
                or workspace_root
            )
            context = {
                "workspace_root": str(Path(workspace_root).resolve()),
                "output_root": str(Path(output_root).resolve()),
                "comms_root": str(Path(workspace_root).resolve() / ".opc-comms"),
            }

    inherited = metadata.get("inherited_environment")
    if isinstance(inherited, dict):
        env_vars = inherited.get("env_vars")
        if isinstance(env_vars, dict) and env_vars:
            existing = dict(context.get("inherited_env_vars", {}) or {})
            existing.update(env_vars)
            context["inherited_env_vars"] = existing
    manifest = metadata.get("environment_manifest")
    if isinstance(manifest, dict):
        env_vars = manifest.get("env_vars")
        if isinstance(env_vars, dict) and env_vars:
            existing = dict(context.get("inherited_env_vars", {}) or {})
            existing.update(env_vars)
            context["inherited_env_vars"] = existing

    active_override = _EXECUTION_CONTEXT_OVERRIDE.get()
    for values in (active_override, override):
        if not values:
            continue
        for key, value in values.items():
            context[str(key)] = dict(value) if isinstance(value, dict) else value

    return context


def resolve_sandbox_config(
    config: OPCConfig | None = None,
    *,
    native_approval_level: str | None = None,
) -> dict[str, Any]:
    platform = platform_key()
    if native_approval_level not in (None, ""):
        profile = native_sandbox_profile(
            normalize_native_approval_level(native_approval_level)
        )
        profile["platform"] = platform
        return profile
    if config is None:
        return {
            "platform": platform,
            "enabled": False,
            "mode": "off",
            "wrapper": "none",
            "fail_if_unavailable": False,
            "allow_direct_fallback": True,
            "allow_network": True,
        }
    sandbox_cfg = config.system.native_runtime.execution_environment.sandbox
    platform_cfg = getattr(sandbox_cfg, platform)
    mode = platform_cfg.mode if platform_cfg.mode != "inherit" else sandbox_cfg.default_mode
    wrapper = platform_cfg.wrapper
    if not sandbox_cfg.enabled:
        mode = "off"
        wrapper = "none"
    return {
        "platform": platform,
        "enabled": bool(sandbox_cfg.enabled),
        "mode": mode,
        "wrapper": wrapper,
        "fail_if_unavailable": bool(sandbox_cfg.fail_if_unavailable),
        "allow_direct_fallback": bool(sandbox_cfg.allow_direct_fallback),
        "allow_network": bool(sandbox_cfg.allow_network),
    }


def build_subprocess_env(context: dict[str, Any] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    context = dict(context or {})

    inherited_env = dict(context.get("inherited_env_vars", {}) or {})
    if inherited_env:
        env.update({str(k): str(v) for k, v in inherited_env.items()})

    venv_path = str(context.get("venv_path", "") or "").strip()
    python_path = str(context.get("python_executable", "") or "").strip()
    if not venv_path or not python_path:
        return env
    if not Path(python_path).exists():
        return env
    bin_dir = str(Path(python_path).resolve().parent)
    env["VIRTUAL_ENV"] = venv_path
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    env.setdefault("UV_PROJECT_ENVIRONMENT", venv_path)
    return env


def resolve_python_executable(context: dict[str, Any] | None = None) -> str:
    context = dict(context or {})
    configured = str(context.get("python_executable", "") or "").strip()
    if configured and Path(configured).exists():
        return configured
    return shutil.which("python3") or shutil.which("python") or sys.executable


def wrap_command_for_context(
    args: list[str],
    *,
    cwd: str,
    context: dict[str, Any] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    context = dict(context or {})
    sandbox = dict(context.get("sandbox", {}) or {})
    mode = str(sandbox.get("mode", "off") or "off").strip().lower() or "off"
    requested_wrapper = str(sandbox.get("wrapper", "auto") or "auto").strip().lower() or "auto"
    platform = str(sandbox.get("platform", "") or platform_key()).strip() or platform_key()
    meta = {
        "platform": platform,
        "requested_mode": mode,
        "effective_mode": mode,
        "requested_wrapper": requested_wrapper,
        "effective_wrapper": "none",
        "available": True,
        "fallback_used": False,
    }
    if mode in {"", "off", "elevated"}:
        return args, meta
    wrapper = requested_wrapper
    if wrapper in {"", "auto"}:
        if platform == "linux" and shutil.which("bwrap"):
            wrapper = "bwrap"
        elif platform == "macos" and shutil.which("sandbox-exec"):
            wrapper = "sandbox-exec"
        else:
            wrapper = "none"
    meta["effective_wrapper"] = wrapper
    if wrapper == "bwrap":
        return _wrap_with_bwrap(args, cwd=cwd, context=context, meta=meta), meta
    if wrapper == "sandbox-exec":
        return _wrap_with_sandbox_exec(args, cwd=cwd, context=context, meta=meta), meta
    return _handle_unavailable_sandbox(args, sandbox=sandbox, meta=meta)


def _handle_unavailable_sandbox(
    args: list[str],
    *,
    sandbox: dict[str, Any],
    meta: dict[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    meta["available"] = False
    allow_direct = bool(sandbox.get("allow_direct_fallback", True))
    if bool(sandbox.get("fail_if_unavailable", False)) and not allow_direct:
        raise RuntimeError(
            f"Sandbox mode `{meta['requested_mode']}` is unavailable on {meta['platform']} and direct fallback is disabled."
        )
    meta["fallback_used"] = True
    meta["effective_mode"] = "off"
    meta["effective_wrapper"] = "none"
    return args, meta


def _wrap_with_bwrap(
    args: list[str],
    *,
    cwd: str,
    context: dict[str, Any],
    meta: dict[str, Any],
) -> list[str]:
    workspace = str(Path(context.get("workspace_root") or cwd).resolve())
    wrapped = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--ro-bind",
        "/",
        "/",
    ]
    sandbox = dict(context.get("sandbox", {}) or {})
    if str(sandbox.get("mode", "") or "").strip().lower() != "read-only":
        wrapped.extend(["--bind", workspace, workspace])
    for raw_path in list(sandbox.get("additional_write_paths", []) or []):
        try:
            write_path = str(Path(raw_path).expanduser().resolve(strict=True))
        except (OSError, RuntimeError):
            continue
        if write_path == workspace or write_path.startswith(f"{workspace}{os.sep}"):
            continue
        wrapped.extend(["--bind", write_path, write_path])
    wrapped.extend([
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--chdir",
        cwd,
    ])
    if not bool(sandbox.get("allow_network", True)):
        wrapped.append("--unshare-net")
    wrapped.extend(args)
    meta["available"] = True
    return wrapped


def _wrap_with_sandbox_exec(
    args: list[str],
    *,
    cwd: str,
    context: dict[str, Any],
    meta: dict[str, Any],
) -> list[str]:
    sandbox = dict(context.get("sandbox", {}) or {})
    workspace = str(Path(context.get("workspace_root") or cwd).resolve())
    escaped_workspace = workspace.replace("\\", "\\\\").replace('"', '\\"')
    allow_network = bool(sandbox.get("allow_network", True))
    profile_lines = [
        "(version 1)",
        "(deny default)",
        "(import \"system.sb\")",
        "(allow process-exec)",
        "(allow process-fork)",
        "(allow signal (target self))",
        "(allow file-read*)",
    ]
    if str(sandbox.get("mode", "") or "").strip().lower() == "read-only":
        profile_lines.append('(allow file-write* (subpath "/tmp") (subpath "/private/tmp"))')
    else:
        profile_lines.append(
            f'(allow file-write* (subpath "{escaped_workspace}") (subpath "/tmp") (subpath "/private/tmp"))'
        )
    for raw_path in list(sandbox.get("additional_write_paths", []) or []):
        try:
            write_path = str(Path(raw_path).expanduser().resolve(strict=True))
        except (OSError, RuntimeError):
            continue
        escaped_path = write_path.replace("\\", "\\\\").replace('"', '\\"')
        profile_lines.append(f'(allow file-write* (subpath "{escaped_path}"))')
    if allow_network:
        profile_lines.append("(allow network*)")
    meta["available"] = True
    return ["sandbox-exec", "-p", "\n".join(profile_lines), *args]

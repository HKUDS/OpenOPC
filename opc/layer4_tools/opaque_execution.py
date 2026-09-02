"""Canonical launch envelopes for company-owned opaque tool effects.

The approval record, durable effect fence, and subprocess handler must all
describe the same launch.  This module is the single builder for that
description.  Secret environment values stay process-local; durable records
contain only canonical digests and key names.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from opc.layer2_organization import shell_safety
from opc.layer2_organization.work_item_identity import (
    work_item_turn_type_from_metadata,
)
from opc.layer2_organization.work_item_links import linked_work_item_id_for_task
from opc.layer4_tools.execution_context import (
    build_subprocess_env,
    resolve_python_executable,
    resolve_task_execution_context,
    wrap_command_for_context,
)


_DEFAULT_SHELL_TIMEOUT = 300
_SETUP_STAGE_DEFAULT_TIMEOUT = 1800
_MAX_APPROVABLE_OPAQUE_PAYLOAD_CHARS = 16_000
_POWERSHELL_CMD_SEPARATOR = " ; "
_BASH_CMD_SEPARATOR = " && "
_PYTHON_STDIN_FILENAME = "<openopc-approved-stdin>"
_SENSITIVE_ENV_NAMES = {
    "PATH",
    "BASH_ENV",
    "ENV",
    "SHELLOPTS",
    "BASHOPTS",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "PYTHONPATH",
    "PYTHONHOME",
}
_INJECTABLE_ENV_NAMES = {
    "BASH_ENV",
    "ENV",
    "SHELLOPTS",
    "BASHOPTS",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
}


class OpaqueExecutionEnvelopeError(RuntimeError):
    """Raised when an exact company launch cannot be resolved."""


def _json_normalize(value: Any) -> Any:
    return json.loads(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _json_normalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _binary_identity(raw_path: str) -> dict[str, Any]:
    """Return an immutable identity for one executable path."""

    path = Path(str(raw_path or "").strip()).expanduser()
    if not path.is_absolute():
        raise OpaqueExecutionEnvelopeError(
            "company executable path is not absolute"
        )
    try:
        real_path = path.resolve(strict=True)
        stat = real_path.stat()
    except OSError as exc:
        raise OpaqueExecutionEnvelopeError(
            f"company executable is unavailable: {path}"
        ) from exc
    if not real_path.is_file():
        raise OpaqueExecutionEnvelopeError(
            f"company executable is not a regular file: {real_path}"
        )
    try:
        content_sha256 = _file_sha256(real_path)
    except OSError as exc:
        raise OpaqueExecutionEnvelopeError(
            f"company executable cannot be fingerprinted: {real_path}"
        ) from exc
    return {
        "path": str(path),
        "realpath": str(real_path),
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "sha256": content_sha256,
    }


def _directory_identity(raw_path: str) -> dict[str, Any]:
    path = Path(str(raw_path or "").strip()).expanduser()
    if not path.is_absolute():
        raise OpaqueExecutionEnvelopeError(
            "company working directory path is not absolute"
        )
    try:
        real_path = path.resolve(strict=True)
        stat = real_path.stat()
    except OSError as exc:
        raise OpaqueExecutionEnvelopeError(
            f"company working directory is unavailable: {path}"
        ) from exc
    if not real_path.is_dir():
        raise OpaqueExecutionEnvelopeError(
            f"company working directory is not a directory: {real_path}"
        )
    return {
        "path": str(path),
        "realpath": str(real_path),
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
    }


def _resolve_launch_executable(raw_path: str, env: Mapping[str, str]) -> str:
    text = str(raw_path or "").strip()
    if not text:
        raise OpaqueExecutionEnvelopeError(
            "company launch wrapper is unavailable"
        )
    if Path(text).is_absolute():
        resolved = _absolute(text)
    else:
        resolved = str(shutil.which(text, path=str(env.get("PATH", "") or "")) or "")
        resolved = _absolute(resolved) if resolved else ""
    if not resolved:
        raise OpaqueExecutionEnvelopeError(
            f"company launch wrapper is unavailable: {text}"
        )
    _binary_identity(resolved)
    return resolved


def _absolute(raw: Any, *, fallback: str = "") -> str:
    text = str(raw or fallback or "").strip()
    if not text:
        return ""
    return str(Path(text).expanduser().resolve(strict=False))


def _task_metadata(task: Any) -> dict[str, Any]:
    return dict(getattr(task, "metadata", {}) or {})


def company_opaque_execution_identity(task: Any) -> dict[str, Any]:
    """Stable company owner identity bound into an exact ToolCall hash.

    A native subagent is represented by its durable child/parent chain.  Its
    parent WorkItem id is copied only as a hint here; the Store effect fence
    independently derives and validates the same value from durable links.
    """

    metadata = _task_metadata(task)
    work_item_id = linked_work_item_id_for_task(task)
    subagent_run_id = str(metadata.get("_comms_endpoint_id", "") or "").strip()
    if subagent_run_id and not work_item_id:
        work_item_id = str(
            metadata.get("_company_parent_work_item_id", "") or ""
        ).strip()
    try:
        attempt_seq = int(
            metadata.get("claimed_work_item_attempt_seq", 0) or 0
        )
    except (TypeError, ValueError):
        attempt_seq = 0
    return {
        "project_id": str(
            getattr(task, "project_id", "") or "default"
        ).strip()
        or "default",
        "run_id": str(metadata.get("delegation_run_id", "") or "").strip(),
        "work_item_id": work_item_id,
        "runtime_task_id": str(getattr(task, "id", "") or "").strip(),
        "subagent_run_id": subagent_run_id,
        "subagent_parent_task_id": str(
            getattr(task, "parent_id", "") or ""
        ).strip(),
        "attempt_seq": attempt_seq,
    }


def _resolved_roots(task: Any, context: Mapping[str, Any]) -> dict[str, str]:
    metadata = _task_metadata(task)
    workspace = str(context.get("workspace_root", "") or "").strip()
    if not workspace:
        workspace = str(
            metadata.get("workspace_root", "")
            or metadata.get("comms_workspace_root", "")
            or metadata.get("target_output_dir", "")
            or ""
        ).strip()
    output = str(context.get("output_root", "") or "").strip()
    if not output:
        output = str(
            metadata.get("output_root", "")
            or metadata.get("target_output_dir", "")
            or workspace
            or ""
        ).strip()
    comms = str(context.get("comms_root", "") or "").strip()
    return {
        "workspace_root": _absolute(workspace),
        "output_root": _absolute(output or workspace),
        "comms_root": _absolute(comms) if comms else "",
    }


def _resolved_cwd(
    arguments: Mapping[str, Any],
    *,
    roots: Mapping[str, str],
) -> str:
    explicit = str(
        arguments.get("working_directory", "")
        or arguments.get("cwd", "")
        or ""
    ).strip()
    if explicit:
        return _absolute(explicit)
    workspace = str(roots.get("workspace_root", "") or "").strip()
    output = str(roots.get("output_root", "") or "").strip()
    return _absolute(workspace or output or os.getcwd())


def _environment_descriptor(
    env: Mapping[str, str],
    inherited: Mapping[str, Any],
) -> dict[str, Any]:
    normalized_env = {str(key): str(value) for key, value in env.items()}
    normalized_inherited = {
        str(key): str(value) for key, value in inherited.items()
    }
    sensitive = {
        key: hashlib.sha256(value.encode("utf-8")).hexdigest()
        for key, value in sorted(normalized_env.items())
        if key in _SENSITIVE_ENV_NAMES
        or key.startswith("GIT_")
        or key.startswith("BASH_FUNC_")
    }
    return {
        "digest": _digest(normalized_env),
        "inherited_keys": sorted(normalized_inherited),
        "inherited_digest": _digest(normalized_inherited),
        "sensitive_value_digests": sensitive,
        "effective_path": normalized_env.get("PATH", ""),
    }


def _company_subprocess_env(
    context: Mapping[str, Any],
    *,
    executable: str,
) -> dict[str, str]:
    """Build a deterministic environment without loader/profile injection."""

    env = build_subprocess_env(dict(context))
    for key in tuple(env):
        upper = str(key).upper()
        if (
            upper in _INJECTABLE_ENV_NAMES
            or upper.startswith("BASH_FUNC_")
            or upper.startswith("GIT_")
            or upper.startswith("DYLD_")
        ):
            env.pop(key, None)

    candidates: list[str] = [str(Path(executable).resolve().parent)]
    configured_python = str(context.get("python_executable", "") or "").strip()
    if configured_python:
        candidates.append(str(Path(configured_python).resolve().parent))
    if os.name == "nt":
        system_root = str(env.get("SystemRoot", "") or "").strip()
        if system_root:
            candidates.extend(
                [system_root, str(Path(system_root) / "System32")]
            )
    else:
        candidates.extend(
            [
                "/usr/local/sbin",
                "/usr/local/bin",
                "/usr/sbin",
                "/usr/bin",
                "/sbin",
                "/bin",
            ]
        )
    trusted: list[str] = []
    for candidate in candidates:
        normalized = _absolute(candidate)
        if normalized and normalized not in trusted and Path(normalized).is_dir():
            trusted.append(normalized)
    if not trusted:
        raise OpaqueExecutionEnvelopeError(
            "company launch has no trusted executable search path"
        )
    env["PATH"] = os.pathsep.join(trusted)
    return env


def _active_shell_prefix(
    task: Any,
    *,
    powershell: bool,
) -> tuple[str, str]:
    metadata = _task_metadata(task)
    inherited = metadata.get("inherited_environment")
    manifest = metadata.get("environment_manifest")
    inherited = dict(inherited) if isinstance(inherited, dict) else {}
    manifest = dict(manifest) if isinstance(manifest, dict) else {}
    prefix = str(inherited.get("shell_prefix", "") or "").strip()
    prefix_win = str(inherited.get("shell_prefix_win", "") or "").strip()
    source = "inherited_environment" if prefix or prefix_win else ""
    if not prefix:
        prefix = str(manifest.get("shell_prefix", "") or "").strip()
        prefix_win = str(manifest.get("shell_prefix_win", "") or "").strip()
        if prefix or prefix_win:
            source = "environment_manifest"
    active = prefix_win if powershell and prefix_win else prefix
    return active, source


def _effective_shell_timeout(task: Any, arguments: Mapping[str, Any]) -> int:
    try:
        timeout = int(arguments.get("timeout", _DEFAULT_SHELL_TIMEOUT) or 0)
    except (TypeError, ValueError):
        timeout = _DEFAULT_SHELL_TIMEOUT
    timeout = max(1, timeout)
    metadata = _task_metadata(task)
    override = metadata.get("shell_timeout_override")
    if override is not None:
        try:
            timeout = max(int(override), timeout)
        except (TypeError, ValueError):
            pass
    elif work_item_turn_type_from_metadata(metadata, fallback="") == "setup":
        timeout = max(timeout, _SETUP_STAGE_DEFAULT_TIMEOUT)
    return timeout


def _python_stdin_bootstrap(cwd: str) -> str:
    """Return the fixed isolated bootstrap for an approved Python payload.

    ``-I`` intentionally removes environment/site injection.  Inserting only
    the frozen working directory preserves the legacy ability to import local
    project modules without creating a shared temporary script that could be
    replaced between approval and execution.
    """

    return (
        "import sys\n"
        f"sys.path.insert(0, {cwd!r})\n"
        "_openopc_source = sys.stdin.buffer.read()\n"
        f"_openopc_filename = {_PYTHON_STDIN_FILENAME!r}\n"
        "_openopc_globals = {"
        "'__name__': '__main__', '__file__': _openopc_filename}"
        "\nexec(compile(_openopc_source, _openopc_filename, 'exec'), "
        "_openopc_globals, _openopc_globals)\n"
    )


def _resolve_shell(arguments: Mapping[str, Any]) -> tuple[str, str]:
    hint = str(arguments.get("shell", "") or "").strip().lower()
    use_powershell = hint == "powershell" or (
        os.name == "nt" and hint not in {"bash", "sh"}
    )
    if use_powershell:
        executable = shutil.which("pwsh") or shutil.which("powershell")
        if not executable:
            raise OpaqueExecutionEnvelopeError(
                "company PowerShell executable is unavailable"
            )
        return "powershell", _absolute(executable)
    executable = shutil.which("bash")
    if executable:
        return "bash", _absolute(executable)
    executable = shutil.which("sh")
    if executable:
        return "sh", _absolute(executable)
    raise OpaqueExecutionEnvelopeError("company shell executable is unavailable")


@dataclass(frozen=True)
class OpaqueExecutionPlan:
    """Process-local immutable launch snapshot.

    ``environment_items`` may contain secrets and is never serialized into a
    checkpoint.  ``envelope_json`` is the durable, secret-free descriptor.
    """

    tool_name: str
    envelope_json: str
    arguments_json: str
    cwd: str
    executable: str
    argv: tuple[str, ...]
    environment_items: tuple[tuple[str, str], ...]
    context_json: str
    sandbox_json: str
    timeout: int
    effective_command: str = ""
    active_prefix: str = ""
    preparation_error: str = ""

    @property
    def envelope(self) -> dict[str, Any]:
        return json.loads(self.envelope_json)

    @property
    def arguments(self) -> dict[str, Any]:
        return json.loads(self.arguments_json)

    @property
    def environment(self) -> dict[str, str]:
        return dict(self.environment_items)

    @property
    def context(self) -> dict[str, Any]:
        return json.loads(self.context_json)

    @property
    def sandbox(self) -> dict[str, Any]:
        return json.loads(self.sandbox_json)


def company_workspace_read_only_shell_decision(
    task: Any,
    arguments: Mapping[str, Any] | None,
    *,
    execution_plan: OpaqueExecutionPlan | None = None,
) -> tuple[bool, str]:
    """Classify the one company shell path that needs no durable permit.

    Prediction uses the lightweight task context. The final effect fence
    passes its frozen plan, so a context change between those boundaries
    fails closed instead of widening the automatic grant.
    """

    normalized_arguments = dict(arguments or {})
    command = str(
        normalized_arguments.get("command", "")
        or normalized_arguments.get("cmd", "")
        or ""
    ).strip()
    if not command:
        return False, "empty shell command"
    try:
        if execution_plan is not None:
            if execution_plan.tool_name != "shell_exec":
                return False, "execution plan is not for shell_exec"
            envelope = execution_plan.envelope
            roots = dict(envelope.get("roots", {}) or {})
            cwd = execution_plan.cwd
            shell_kind = str(envelope.get("shell_kind", "") or "")
            active_prefix = execution_plan.active_prefix
            if execution_plan.preparation_error:
                return False, execution_plan.preparation_error
        else:
            context = _json_normalize(resolve_task_execution_context(task))
            roots = _resolved_roots(task, context)
            cwd = _resolved_cwd(normalized_arguments, roots=roots)
            shell_kind, _ = _resolve_shell(normalized_arguments)
            active_prefix, _ = _active_shell_prefix(
                task,
                powershell=shell_kind == "powershell",
            )
    except (
        OpaqueExecutionEnvelopeError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        return False, f"company shell context cannot be resolved safely: {exc}"
    if shell_kind not in {"bash", "sh"}:
        return False, "only audited POSIX shell inspection can bypass approval"
    if active_prefix:
        return False, "shell environment prefix requires exact approval"
    return shell_safety.is_workspace_scoped_read_only_shell_command(
        command,
        working_directory=cwd,
        workspace_root=str(roots.get("workspace_root", "") or ""),
    )


def build_company_opaque_execution_plan(
    task: Any,
    tool_name: str,
    arguments: Mapping[str, Any] | None,
    *,
    sandbox_override: Mapping[str, Any] | None = None,
) -> OpaqueExecutionPlan:
    """Resolve the exact launch that approval and the handler must share."""

    name = str(tool_name or "").strip()
    if name not in {"shell_exec", "python_exec"}:
        raise OpaqueExecutionEnvelopeError(
            f"unsupported opaque company tool {name!r}"
        )
    normalized_arguments = _json_normalize(dict(arguments or {}))
    context = _json_normalize(
        resolve_task_execution_context(
            task,
            override=(
                {"sandbox": dict(sandbox_override)}
                if sandbox_override
                else None
            ),
        )
    )
    roots = _resolved_roots(task, context)
    cwd = _resolved_cwd(normalized_arguments, roots=roots)
    inherited_env = dict(context.get("inherited_env_vars", {}) or {})
    timeout = (
        _effective_shell_timeout(task, normalized_arguments)
        if name == "shell_exec"
        else max(1, int(normalized_arguments.get("timeout", 60) or 60))
    )
    preparation_error = ""

    if name == "shell_exec":
        raw_command = str(
            normalized_arguments.get("command", "")
            or normalized_arguments.get("cmd", "")
            or ""
        )
        shell_kind, executable = _resolve_shell(normalized_arguments)
        active_prefix, prefix_source = _active_shell_prefix(
            task,
            powershell=shell_kind == "powershell",
        )
        effective_command = raw_command
        if active_prefix and active_prefix not in raw_command:
            separator = (
                _POWERSHELL_CMD_SEPARATOR
                if shell_kind == "powershell"
                else _BASH_CMD_SEPARATOR
            )
            effective_command = f"{active_prefix}{separator}{raw_command}"
        if len(effective_command) > _MAX_APPROVABLE_OPAQUE_PAYLOAD_CHARS:
            raise OpaqueExecutionEnvelopeError(
                "company shell payload exceeds the exact approval display limit"
            )
        if shell_kind == "powershell":
            base_argv = [
                executable,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                effective_command,
            ]
        elif shell_kind == "bash":
            base_argv = [
                executable,
                "--noprofile",
                "--norc",
                "-c",
                effective_command,
            ]
        else:
            base_argv = [executable, "-c", effective_command]
    else:
        raw_command = ""
        active_prefix = ""
        prefix_source = ""
        context_python = str(context.get("python_executable", "") or "").strip()
        python_code = str(normalized_arguments.get("code", "") or "")
        if len(python_code) > _MAX_APPROVABLE_OPAQUE_PAYLOAD_CHARS:
            raise OpaqueExecutionEnvelopeError(
                "company Python payload exceeds the exact approval display limit"
            )
        executable = _absolute(resolve_python_executable(context))
        if not executable or not Path(executable).exists():
            raise OpaqueExecutionEnvelopeError(
                "company Python executable is unavailable"
            )
        if context_python and _absolute(context_python) != executable:
            raise OpaqueExecutionEnvelopeError(
                "company Python executable cannot be resolved exactly"
            )
        effective_command = ""
        # The approved Python payload is delivered over stdin.  A temporary
        # script inside a shared workspace would create an unowned mutation
        # and a close/reopen race between the approved bytes and execution.
        base_argv = [executable, "-I", "-c", _python_stdin_bootstrap(cwd)]

    env = _company_subprocess_env(context, executable=executable)
    environment_descriptor = _environment_descriptor(env, inherited_env)

    if not cwd or not Path(cwd).exists() or not Path(cwd).is_dir():
        preparation_error = f"Working directory does not exist: {cwd}"
        wrapped_argv = list(base_argv)
        sandbox_meta = {
            "platform": str(dict(context.get("sandbox", {}) or {}).get("platform", "")),
            "requested_mode": str(dict(context.get("sandbox", {}) or {}).get("mode", "")),
            "effective_mode": "off",
            "available": False,
            "fallback_used": False,
        }
    else:
        try:
            wrapped_argv, sandbox_meta = wrap_command_for_context(
                list(base_argv),
                cwd=cwd,
                context=context,
            )
        except RuntimeError as exc:
            preparation_error = str(exc)
            wrapped_argv = list(base_argv)
            sandbox_meta = {
                "platform": str(
                    dict(context.get("sandbox", {}) or {}).get("platform", "")
                ),
                "requested_mode": str(
                    dict(context.get("sandbox", {}) or {}).get("mode", "")
                ),
                "effective_mode": "off",
                "available": False,
                "fallback_used": False,
            }

    wrapped_argv = [str(item) for item in wrapped_argv]
    wrapped_argv[0] = _resolve_launch_executable(wrapped_argv[0], env)
    binary_identities = {
        "launch": _binary_identity(wrapped_argv[0]),
        "inner": _binary_identity(executable),
    }

    envelope = {
        "schema_version": 1,
        "tool_name": name,
        "shell_kind": shell_kind if name == "shell_exec" else "python",
        "executable": executable,
        "argv": list(wrapped_argv),
        "cwd": cwd,
        "roots": roots,
        "timeout": timeout,
        "raw_command": raw_command,
        "effective_command": effective_command,
        "python_code_sha256": (
            hashlib.sha256(
                str(normalized_arguments.get("code", "") or "").encode("utf-8")
            ).hexdigest()
            if name == "python_exec"
            else ""
        ),
        "python_code_preview": (
            str(normalized_arguments.get("code", "") or "")
            if name == "python_exec"
            else ""
        ),
        "active_prefix": active_prefix,
        "prefix_source": prefix_source,
        "environment": environment_descriptor,
        "execution_context": {
            "workspace_root": roots["workspace_root"],
            "output_root": roots["output_root"],
            "comms_root": roots["comms_root"],
            "venv_path": _absolute(context.get("venv_path", "")),
            "python_executable": _absolute(
                context.get("python_executable", "")
            ),
            "venv_provider": str(context.get("venv_provider", "") or ""),
            "preparation_error": str(
                context.get("preparation_error", "") or ""
            ),
            "sandbox": _json_normalize(context.get("sandbox", {}) or {}),
        },
        "sandbox_resolution": _json_normalize(sandbox_meta),
        "binary_identities": binary_identities,
        "cwd_identity": _directory_identity(cwd),
        "preparation_error": preparation_error,
    }
    return OpaqueExecutionPlan(
        tool_name=name,
        envelope_json=_canonical_json(envelope),
        arguments_json=_canonical_json(normalized_arguments),
        cwd=cwd,
        executable=executable,
        argv=tuple(str(item) for item in wrapped_argv),
        environment_items=tuple(sorted((str(k), str(v)) for k, v in env.items())),
        context_json=_canonical_json(context),
        sandbox_json=_canonical_json(sandbox_meta),
        timeout=timeout,
        effective_command=effective_command,
        active_prefix=active_prefix,
        preparation_error=preparation_error,
    )


def company_opaque_execution_plan_for_permit(
    task: Any,
    tool_name: str,
    arguments: Mapping[str, Any] | None,
    *,
    permit_envelope: Mapping[str, Any] | None,
    sandbox_override: Mapping[str, Any] | None = None,
) -> OpaqueExecutionPlan:
    """Rebuild a plan from its frozen permit, or from current policy pre-permit."""

    frozen = _json_normalize(dict(permit_envelope or {}))
    if frozen:
        sandbox_override = dict(
            dict(frozen.get("execution_context", {}) or {}).get("sandbox", {})
            or {}
        ) or None
    return build_company_opaque_execution_plan(
        task,
        tool_name,
        arguments,
        sandbox_override=sandbox_override,
    )


def activate_approved_opaque_execution_plan(
    plan: OpaqueExecutionPlan,
    *,
    sandbox_override: Mapping[str, Any] | None,
) -> OpaqueExecutionPlan:
    """Make an explicitly approved missing-wrapper launch executable.

    When a sandbox wrapper is unavailable, the frozen envelope already
    contains the exact direct argv shown to the owner.  Approval may therefore
    clear only that policy preparation error; executable, argv, cwd,
    environment, and the durable envelope remain unchanged.
    """

    override = _json_normalize(dict(sandbox_override or {}))
    resolution = dict(plan.sandbox or {})
    if (
        not plan.preparation_error
        or not plan.preparation_error.startswith("Sandbox mode `")
        or bool(resolution.get("available", True))
        or str(resolution.get("effective_mode", "") or "").strip().lower()
        != "off"
        or str(override.get("mode", "") or "").strip().lower()
        not in {"off", "elevated"}
        or not bool(override.get("allow_direct_fallback", False))
    ):
        return plan
    context = dict(plan.context or {})
    context["sandbox"] = override
    effective_resolution = {
        **resolution,
        "effective_mode": "off",
        "effective_wrapper": "none",
        "fallback_used": True,
    }
    return replace(
        plan,
        context_json=_canonical_json(context),
        sandbox_json=_canonical_json(effective_resolution),
        preparation_error="",
    )


def exact_tool_call_fingerprint(
    *,
    tool_call_id: str,
    tool_name: str,
    arguments: Mapping[str, Any] | None,
    runtime_session_id: str,
    execution_envelope: Mapping[str, Any] | None = None,
    execution_identity: Mapping[str, Any] | None = None,
) -> str:
    """Hash one immutable ToolCall and its optional exact launch envelope."""

    basis: dict[str, Any] = {
        "tool_call_id": str(tool_call_id or ""),
        "tool_name": str(tool_name or ""),
        "arguments": _json_normalize(dict(arguments or {})),
        "runtime_session_id": str(runtime_session_id or ""),
    }
    if execution_envelope:
        basis["execution_envelope"] = _json_normalize(
            dict(execution_envelope)
        )
    if execution_identity:
        basis["execution_identity"] = _json_normalize(
            dict(execution_identity)
        )
    return hashlib.sha256(_canonical_json(basis).encode("utf-8")).hexdigest()


def opaque_execution_envelope_digest(
    execution_envelope: Mapping[str, Any] | None,
) -> str:
    """Return the canonical digest persisted beside exact ToolCall evidence."""

    return _digest(dict(execution_envelope or {}))


_CURRENT_PLAN: contextvars.ContextVar[OpaqueExecutionPlan | None] = (
    contextvars.ContextVar("opc_company_opaque_execution_plan", default=None)
)


def install_opaque_execution_plan(plan: OpaqueExecutionPlan):
    """Install *plan* for exactly one handler call; caller must reset token."""

    return _CURRENT_PLAN.set(plan)


def reset_opaque_execution_plan(token: contextvars.Token) -> None:
    _CURRENT_PLAN.reset(token)


def current_opaque_execution_plan(tool_name: str) -> OpaqueExecutionPlan | None:
    plan = _CURRENT_PLAN.get()
    if plan is None:
        return None
    if plan.tool_name != str(tool_name or "").strip():
        raise OpaqueExecutionEnvelopeError(
            "opaque execution plan belongs to a different tool"
        )
    return plan


def verify_opaque_execution_plan(plan: OpaqueExecutionPlan) -> None:
    """Fail closed if an approved executable or cwd changed before launch."""

    expected = dict(plan.envelope.get("binary_identities", {}) or {})
    if set(expected) != {"launch", "inner"}:
        raise OpaqueExecutionEnvelopeError(
            "company launch has an incomplete executable identity"
        )
    for role in ("launch", "inner"):
        recorded = dict(expected.get(role, {}) or {})
        path = str(recorded.get("path", "") or "").strip()
        if not path or _binary_identity(path) != recorded:
            raise OpaqueExecutionEnvelopeError(
                f"company {role} executable changed after approval"
            )
    recorded_cwd = dict(plan.envelope.get("cwd_identity", {}) or {})
    cwd_path = str(recorded_cwd.get("path", "") or "").strip()
    if not cwd_path or _directory_identity(cwd_path) != recorded_cwd:
        raise OpaqueExecutionEnvelopeError(
            "company working directory changed after approval"
        )


def opaque_envelope_display(envelope: Mapping[str, Any] | None) -> dict[str, Any]:
    """Secret-free operator-facing summary of a frozen launch envelope."""

    payload = dict(envelope or {})
    if not payload:
        return {}
    environment = dict(payload.get("environment", {}) or {})
    roots = dict(payload.get("roots", {}) or {})
    return {
        "effective_command": str(payload.get("effective_command", "") or ""),
        "cwd": str(payload.get("cwd", "") or ""),
        "shell_kind": str(payload.get("shell_kind", "") or ""),
        "executable": str(payload.get("executable", "") or ""),
        "active_prefix": str(payload.get("active_prefix", "") or ""),
        "inherited_environment_keys": list(
            environment.get("inherited_keys", []) or []
        ),
        "sensitive_environment_value_digests": dict(
            environment.get("sensitive_value_digests", {}) or {}
        ),
        "effective_path": str(environment.get("effective_path", "") or ""),
        "workspace_root": str(roots.get("workspace_root", "") or ""),
        "output_root": str(roots.get("output_root", "") or ""),
        "python_code_sha256": str(
            payload.get("python_code_sha256", "") or ""
        ),
        "python_code_preview": str(
            payload.get("python_code_preview", "") or ""
        ),
    }

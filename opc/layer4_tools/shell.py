"""Structured shell execution tools."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any, AsyncIterator

from opc.layer4_tools.execution_context import (
    build_subprocess_env,
    resolve_task_execution_context,
    wrap_command_for_context,
)
from opc.layer4_tools.opaque_execution import (
    OpaqueExecutionEnvelopeError,
    OpaqueExecutionPlan,
    current_opaque_execution_plan,
    verify_opaque_execution_plan,
)
from opc.layer4_tools.registry import COMPANY_EFFECT_OPAQUE_EXACT, ToolDefinition
from opc.layer2_organization.work_item_identity import work_item_turn_type_from_metadata


_STDOUT_LIMIT = 50_000
_STDERR_LIMIT = 20_000
_SETUP_STAGE_DEFAULT_TIMEOUT = 1800
_DEFAULT_SHELL_TIMEOUT = 300
_POWERSHELL_CMD_SEPARATOR = " ; "
_BASH_CMD_SEPARATOR = " && "
_STREAM_READ_SIZE = 8192


def _resolve_working_directory(
    working_directory: str | None = None,
    task: Any | None = None,
) -> str:
    cwd = str(working_directory or "").strip()
    if not cwd and task is not None:
        metadata = getattr(task, "metadata", {}) or {}
        execution_context = dict(metadata.get("_execution_context", {}) or {})
        candidates = [
            str(execution_context.get("workspace_root", "") or "").strip(),
            str(execution_context.get("output_root", "") or "").strip(),
            str(metadata.get("workspace_root", "") or "").strip(),
            str(metadata.get("comms_workspace_root", "") or "").strip(),
            str(metadata.get("output_root", "") or "").strip(),
            str(metadata.get("target_output_dir", "") or "").strip(),
        ]
        fallback = ""
        for raw in candidates:
            if not raw:
                continue
            path = Path(raw).expanduser()
            if path.exists() and path.is_dir():
                return str(path)
            if not fallback:
                fallback = str(path)
        cwd = fallback
    return cwd or os.getcwd()


def _shell_binary(preferred: str, fallback: str) -> str:
    return shutil.which(preferred) or fallback


async def _run_shell_command(
    *,
    shell_name: str,
    command: str,
    args: list[str],
    working_directory: str | None = None,
    timeout: int = _DEFAULT_SHELL_TIMEOUT,
    task: Any | None = None,
    on_progress: Any = None,
    frozen_plan: OpaqueExecutionPlan | None = None,
) -> dict[str, Any]:
    if frozen_plan is not None:
        resolved_cwd = frozen_plan.cwd
        context = frozen_plan.context
        command = frozen_plan.effective_command
        args = list(frozen_plan.argv)
        env = frozen_plan.environment
        sandbox_meta = frozen_plan.sandbox
        timeout = frozen_plan.timeout
        preparation_error = frozen_plan.preparation_error
    else:
        cwd = _resolve_working_directory(working_directory, task)
        cwd_path = Path(cwd).expanduser()
        resolved_cwd = str(cwd_path.resolve(strict=False))
        preparation_error = ""
        if not cwd_path.exists() or not cwd_path.is_dir():
            preparation_error = f"Working directory does not exist: {resolved_cwd}"
        if task is not None:
            meta = getattr(task, "metadata", {}) or {}
            override = meta.get("shell_timeout_override")
            if override is not None:
                try:
                    timeout = max(int(override), timeout)
                except (ValueError, TypeError):
                    pass
            elif work_item_turn_type_from_metadata(meta, fallback="") == "setup":
                timeout = max(timeout, _SETUP_STAGE_DEFAULT_TIMEOUT)
            shell_prefix = ""
            shell_prefix_win = ""
            inherited = meta.get("inherited_environment")
            if isinstance(inherited, dict):
                shell_prefix = str(inherited.get("shell_prefix", "") or "").strip()
                shell_prefix_win = str(inherited.get("shell_prefix_win", "") or "").strip()
            if not shell_prefix:
                manifest = meta.get("environment_manifest")
                if isinstance(manifest, dict):
                    shell_prefix = str(manifest.get("shell_prefix", "") or "").strip()
                    shell_prefix_win = str(manifest.get("shell_prefix_win", "") or "").strip()
            is_powershell = shell_name == "powershell"
            active_prefix = shell_prefix_win if (is_powershell and shell_prefix_win) else shell_prefix
            if active_prefix and active_prefix not in command:
                separator = _POWERSHELL_CMD_SEPARATOR if is_powershell else _BASH_CMD_SEPARATOR
                command = f"{active_prefix}{separator}{command}"
                # Replace only the trailing command argument. PowerShell args are
                # [exe, "-NoProfile", "-Command", command] (4 elements); the previous
                # ``[args[0], args[1], command]`` dropped the "-Command" flag and left
                # PowerShell unable to interpret the prefixed command. Bash args are
                # [bash, "-lc", command] (3 elements), handled identically here.
                args = [*args[:-1], command] if len(args) >= 1 else args
        context = resolve_task_execution_context(task)
        if resolved_cwd and not context.get("workspace_root"):
            context["workspace_root"] = resolved_cwd
        env = build_subprocess_env(context)
        try:
            args, sandbox_meta = wrap_command_for_context(
                args,
                cwd=resolved_cwd,
                context=context,
            )
        except RuntimeError as exc:
            preparation_error = str(exc)
            sandbox_cfg = dict(context.get("sandbox", {}) or {})
            sandbox_meta = {
                "platform": str(sandbox_cfg.get("platform", "") or ""),
                "requested_mode": str(sandbox_cfg.get("mode", "") or ""),
                "effective_mode": "off",
                "available": False,
                "fallback_used": False,
            }

    if preparation_error:
        return {
            "success": False,
            "shell": shell_name,
            "command": command,
            "cwd": resolved_cwd,
            "stdout": "",
            "stderr": "",
            "exit_code": -1,
            "timed_out": False,
            "error": preparation_error,
            "sandbox": sandbox_meta,
            "execution_context": _context_preview(context),
        }
    if frozen_plan is not None:
        try:
            verify_opaque_execution_plan(frozen_plan)
        except OpaqueExecutionEnvelopeError as exc:
            return {
                "success": False,
                "shell": shell_name,
                "command": command,
                "cwd": resolved_cwd,
                "stdout": "",
                "stderr": "",
                "exit_code": -1,
                "timed_out": False,
                "error": str(exc),
                "sandbox": sandbox_meta,
                "execution_context": _context_preview(context),
            }
    wrapped_args = list(args)
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *wrapped_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=resolved_cwd,
            env=env,
        )
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []

        async def _pump(stream: Any, bucket: list[str], stream_name: str, limit: int) -> None:
            async for chunk in _iter_stream_lines(stream):
                text = chunk.decode("utf-8", errors="replace")
                bucket.append(text)
                joined = "".join(bucket)
                if len(joined) > limit:
                    bucket[:] = [joined[:limit]]
                if on_progress:
                    try:
                        await on_progress(text.rstrip("\r\n"), stream=stream_name)
                    except TypeError:
                        await on_progress(text.rstrip("\r\n"))

        stdout_task = asyncio.create_task(_pump(proc.stdout, stdout_chunks, "stdout", _STDOUT_LIMIT))
        stderr_task = asyncio.create_task(_pump(proc.stderr, stderr_chunks, "stderr", _STDERR_LIMIT))
        await asyncio.wait_for(proc.wait(), timeout=timeout)
        await asyncio.gather(stdout_task, stderr_task)
        stdout = "".join(stdout_chunks)[:_STDOUT_LIMIT]
        stderr = "".join(stderr_chunks)[:_STDERR_LIMIT]
        return {
            "success": proc.returncode == 0,
            "shell": shell_name,
            "command": command,
            "cwd": resolved_cwd,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": proc.returncode,
            "timed_out": False,
            "sandbox": sandbox_meta,
            "execution_context": _context_preview(context),
        }
    except asyncio.TimeoutError:
        if proc is not None:
            proc.kill()
        return {
            "success": False,
            "shell": shell_name,
            "command": command,
            "cwd": resolved_cwd,
            "stdout": "",
            "stderr": "",
            "exit_code": -1,
            "timed_out": True,
            "error": f"Command timed out after {timeout}s",
            "sandbox": sandbox_meta,
            "execution_context": _context_preview(context),
        }


async def _iter_stream_lines(stream: asyncio.StreamReader) -> AsyncIterator[bytes]:
    buffer = bytearray()
    while True:
        chunk = await stream.read(_STREAM_READ_SIZE)
        if not chunk:
            if buffer:
                yield bytes(buffer)
            return
        buffer.extend(chunk)
        while True:
            newline_index = buffer.find(b"\n")
            if newline_index < 0:
                break
            line = bytes(buffer[: newline_index + 1])
            del buffer[: newline_index + 1]
            yield line


def _context_preview(context: dict[str, Any]) -> dict[str, Any]:
    sandbox = dict(context.get("sandbox", {}) or {})
    return {
        "workspace_root": str(context.get("workspace_root", "") or ""),
        "output_root": str(context.get("output_root", "") or ""),
        "comms_root": str(context.get("comms_root", "") or ""),
        "venv_path": str(context.get("venv_path", "") or ""),
        "python_executable": str(context.get("python_executable", "") or ""),
        "venv_provider": str(context.get("venv_provider", "") or ""),
        "preparation_error": str(context.get("preparation_error", "") or ""),
        "sandbox": sandbox,
    }


async def bash_exec(
    command: str,
    working_directory: str | None = None,
    timeout: int = _DEFAULT_SHELL_TIMEOUT,
    task: Any | None = None,
    on_progress: Any = None,
) -> dict[str, Any]:
    """Execute a command using bash/sh semantics."""
    shell_binary = _shell_binary("bash", "sh" if os.name != "nt" else "bash")
    return await _run_shell_command(
        shell_name="bash",
        command=command,
        args=[shell_binary, "-lc", command],
        working_directory=working_directory,
        timeout=timeout,
        task=task,
        on_progress=on_progress,
    )


async def powershell_exec(
    command: str,
    working_directory: str | None = None,
    timeout: int = _DEFAULT_SHELL_TIMEOUT,
    task: Any | None = None,
    on_progress: Any = None,
) -> dict[str, Any]:
    """Execute a command using PowerShell semantics."""
    executable = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
    return await _run_shell_command(
        shell_name="powershell",
        command=command,
        args=[executable, "-NoProfile", "-Command", command],
        working_directory=working_directory,
        timeout=timeout,
        task=task,
        on_progress=on_progress,
    )


async def shell_exec(
    command: str,
    working_directory: str | None = None,
    timeout: int = _DEFAULT_SHELL_TIMEOUT,
    shell: str | None = None,
    task: Any | None = None,
    on_progress: Any = None,
) -> dict[str, Any]:
    """Compatibility wrapper that selects bash or PowerShell."""
    frozen_plan = current_opaque_execution_plan("shell_exec")
    if frozen_plan is not None:
        return await _run_shell_command(
            shell_name=str(
                frozen_plan.envelope.get("shell_kind", "") or "shell"
            ),
            command=command,
            args=list(frozen_plan.argv),
            working_directory=working_directory,
            timeout=timeout,
            task=None,
            on_progress=on_progress,
            frozen_plan=frozen_plan,
        )
    normalized = str(shell or "").strip().lower()
    if normalized == "powershell":
        return await powershell_exec(
            command=command,
            working_directory=working_directory,
            timeout=timeout,
            task=task,
            on_progress=on_progress,
        )
    if os.name == "nt" and normalized not in {"bash", "sh"}:
        return await powershell_exec(
            command=command,
            working_directory=working_directory,
            timeout=timeout,
            task=task,
            on_progress=on_progress,
        )
    return await bash_exec(
        command=command,
        working_directory=working_directory,
        timeout=timeout,
        task=task,
        on_progress=on_progress,
    )


def _shell_schema(description: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": description},
            "working_directory": {"type": "string", "description": "Working directory for the command (optional)"},
            "timeout": {"type": "integer", "description": "Timeout in seconds", "default": _DEFAULT_SHELL_TIMEOUT},
        },
        "required": ["command"],
    }


def create_shell_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="shell_exec",
            description="Execute a shell command. Selects bash or PowerShell based on platform or the optional `shell` hint.",
            parameters={
                "type": "object",
                "properties": {
                    **_shell_schema("The shell command to execute")["properties"],
                    "shell": {
                        "type": "string",
                        "description": "Optional shell hint: bash | powershell",
                        "default": "",
                    },
                },
                "required": ["command"],
            },
            func=shell_exec,
            category="compute",
            requires_confirmation=True,
            concurrency_safe=False,
            read_only=False,
            company_effect_kind=COMPANY_EFFECT_OPAQUE_EXACT,
        ),
    ]


def create_shell_tool() -> ToolDefinition:
    """Backward-compatible helper used by older callers."""
    for tool in create_shell_tools():
        if tool.name == "shell_exec":
            return tool
    raise RuntimeError("shell_exec definition is missing")

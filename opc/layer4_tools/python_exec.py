"""Python code execution tool."""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any

from opc.layer4_tools.execution_context import (
    build_subprocess_env,
    resolve_python_executable,
    resolve_task_execution_context,
    wrap_command_for_context,
)
from opc.layer4_tools.output_budget import clip_text, persist_tool_result
from opc.layer4_tools.opaque_execution import (
    OpaqueExecutionEnvelopeError,
    current_opaque_execution_plan,
    verify_opaque_execution_plan,
)
from opc.layer4_tools.registry import COMPANY_EFFECT_OPAQUE_EXACT, ToolDefinition


async def python_exec(
    code: str,
    timeout: int = 60,
    task: Any | None = None,
    on_progress: Any = None,
) -> dict[str, Any]:
    """Execute Python code in a subprocess and capture output."""
    _ = on_progress
    frozen_plan = current_opaque_execution_plan("python_exec")
    if frozen_plan is not None:
        context = frozen_plan.context
        roots = dict(frozen_plan.envelope.get("roots", {}) or {})
        workspace_root = str(roots.get("workspace_root", "") or "").strip()
        if frozen_plan.preparation_error:
            return {
                "error": frozen_plan.preparation_error,
                "exit_code": -1,
                "execution_context": {
                    "workspace_root": workspace_root,
                    "python_executable": frozen_plan.executable,
                    "sandbox": frozen_plan.sandbox,
                },
            }
        timeout = frozen_plan.timeout
        expected_code_digest = str(
            frozen_plan.envelope.get("python_code_sha256", "") or ""
        )
        actual_code_digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
        if expected_code_digest != actual_code_digest:
            return {
                "error": "company Python payload changed after approval",
                "exit_code": -1,
                "execution_context": {
                    "workspace_root": workspace_root,
                    "python_executable": frozen_plan.executable,
                    "sandbox": frozen_plan.sandbox,
                },
            }
        try:
            verify_opaque_execution_plan(frozen_plan)
        except OpaqueExecutionEnvelopeError as exc:
            return {
                "error": str(exc),
                "exit_code": -1,
                "execution_context": {
                    "workspace_root": workspace_root,
                    "python_executable": frozen_plan.executable,
                    "sandbox": frozen_plan.sandbox,
                },
            }
    else:
        context = resolve_task_execution_context(task)
        workspace_root = str(context.get("workspace_root", "") or "").strip()
    tmp_path: str | None = None
    if frozen_plan is None:
        temp_dir = (
            workspace_root
            if workspace_root and Path(workspace_root).exists()
            else None
        )
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".py",
            delete=False,
            dir=temp_dir,
        ) as handle:
            handle.write(code)
            tmp_path = handle.name

    proc: asyncio.subprocess.Process | None = None
    try:
        if frozen_plan is not None:
            executable = frozen_plan.executable
            cwd = frozen_plan.cwd
            env = frozen_plan.environment
            wrapped_args = list(frozen_plan.argv)
            sandbox_meta = frozen_plan.sandbox
            stdin = asyncio.subprocess.PIPE
            input_bytes = code.encode("utf-8")
        else:
            assert tmp_path is not None
            executable = resolve_python_executable(context)
            cwd = workspace_root or os.getcwd()
            env = build_subprocess_env(context)
            wrapped_args, sandbox_meta = wrap_command_for_context(
                [executable, tmp_path],
                cwd=str(Path(cwd).resolve()),
                context=context,
            )
            stdin = None
            input_bytes = None
        proc = await asyncio.create_subprocess_exec(
            *wrapped_args,
            stdin=stdin,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=(cwd if frozen_plan is not None else str(Path(cwd).resolve())),
            env=env,
        )
        communicate = (
            proc.communicate(input=input_bytes)
            if frozen_plan is not None
            else proc.communicate()
        )
        stdout, stderr = await asyncio.wait_for(
            communicate,
            timeout=timeout,
        )
        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        stdout_clip = clip_text(stdout_text, limit=30000, marker="python stdout truncated")
        stderr_clip = clip_text(stderr_text, limit=10000, marker="python stderr truncated")
        stdout_persisted = (
            persist_tool_result(stdout_text, tool_name="python_exec_stdout", task=task, extension="txt")
            if stdout_clip.truncated else {}
        )
        stderr_persisted = (
            persist_tool_result(stderr_text, tool_name="python_exec_stderr", task=task, extension="txt")
            if stderr_clip.truncated else {}
        )
        return {
            "success": proc.returncode == 0,
            "stdout": stdout_clip.text,
            "stderr": stderr_clip.text,
            "stdout_truncated": stdout_clip.truncated,
            "stdout_omitted_chars": stdout_clip.omitted_chars,
            "stderr_truncated": stderr_clip.truncated,
            "stderr_omitted_chars": stderr_clip.omitted_chars,
            "full_stdout_path": stdout_persisted.get("full_output_path", ""),
            "full_stderr_path": stderr_persisted.get("full_output_path", ""),
            "exit_code": proc.returncode,
            "sandbox": sandbox_meta,
            "execution_context": {
                "workspace_root": workspace_root,
                "venv_path": str(context.get("venv_path", "") or ""),
                "python_executable": executable,
                "preparation_error": str(context.get("preparation_error", "") or ""),
                "sandbox": dict(context.get("sandbox", {}) or {}),
            },
        }
    except asyncio.TimeoutError:
        if proc is not None:
            proc.kill()
        return {
            "error": f"Execution timed out after {timeout}s",
            "exit_code": -1,
            "execution_context": {
                "workspace_root": workspace_root,
                "venv_path": str(context.get("venv_path", "") or ""),
                "python_executable": (
                    frozen_plan.executable
                    if frozen_plan is not None
                    else resolve_python_executable(context)
                ),
                "preparation_error": str(context.get("preparation_error", "") or ""),
            },
        }
    except RuntimeError as exc:
        return {
            "error": str(exc),
            "exit_code": -1,
            "execution_context": {
                "workspace_root": workspace_root,
                "venv_path": str(context.get("venv_path", "") or ""),
                "python_executable": (
                    frozen_plan.executable
                    if frozen_plan is not None
                    else resolve_python_executable(context)
                ),
                "preparation_error": str(context.get("preparation_error", "") or ""),
                "sandbox": dict(context.get("sandbox", {}) or {}),
            },
        }
    finally:
        if tmp_path is not None:
            Path(tmp_path).unlink(missing_ok=True)


def create_python_tool() -> ToolDefinition:
    return ToolDefinition(
        name="python_exec",
        description="Execute Python code and return stdout/stderr. Use for data processing, calculations, or testing.",
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute"},
                "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 60},
            },
            "required": ["code"],
        },
        func=python_exec,
        category="compute",
        self_bounded_output=True,
        max_result_chars=80_000,
        company_effect_kind=COMPANY_EFFECT_OPAQUE_EXACT,
        permission_effects=["process_execute", "workspace_write"],
    )

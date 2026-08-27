"""Shared helpers for recoverable tool-output previews."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from opc.core.config import get_opc_home
from opc.layer4_tools.execution_context import resolve_task_execution_context
from opc.layer4_tools.workspace_fs import SecureWorkspace, workspace_roots_for_task


DEFAULT_TRUNCATION_MARKER = "truncated"


@dataclass(frozen=True)
class TextClip:
    text: str
    truncated: bool
    omitted_chars: int
    original_chars: int
    kept_chars: int


def clip_text(
    value: Any,
    *,
    limit: int,
    marker: str = DEFAULT_TRUNCATION_MARKER,
    prefer_newline: bool = True,
) -> TextClip:
    """Return a marked preview of ``value`` within a character budget."""
    text = str(value or "")
    original_chars = len(text)
    limit = max(0, int(limit or 0))
    if original_chars <= limit:
        return TextClip(
            text=text,
            truncated=False,
            omitted_chars=0,
            original_chars=original_chars,
            kept_chars=original_chars,
        )
    if limit <= 0:
        kept = ""
    else:
        kept = text[:limit]
        if prefer_newline and "\n" in kept:
            floor = max(1, int(limit * 0.80))
            boundary = kept.rfind("\n", floor)
            if boundary > 0:
                kept = kept[:boundary]
    kept = kept.rstrip()
    omitted = original_chars - len(kept)
    marker_text = marker.strip("[]") or DEFAULT_TRUNCATION_MARKER
    suffix = f"[{marker_text}: {omitted} chars omitted]"
    rendered = f"{kept}\n{suffix}" if kept else suffix
    return TextClip(
        text=rendered,
        truncated=True,
        omitted_chars=omitted,
        original_chars=original_chars,
        kept_chars=len(kept),
    )


def truncation_metadata(clip: TextClip, *, prefix: str = "") -> dict[str, Any]:
    """Render stable metadata keys for a text clip."""
    return {
        f"{prefix}truncated": clip.truncated,
        f"{prefix}omitted_chars": clip.omitted_chars,
        f"{prefix}original_chars": clip.original_chars,
    }


def _safe_segment(value: str, fallback: str) -> str:
    segment = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip(".-")
    return segment[:80] or fallback


def _tool_results_root(task: Any | None) -> Path:
    context = resolve_task_execution_context(task)
    comms_root = str(context.get("comms_root", "") or "").strip()
    if comms_root:
        # Do not resolve here: following a pre-existing ``.opc-comms`` symlink
        # would escape the task workspace before the secure writer can reject
        # it.
        return Path(comms_root).expanduser() / "tool-results"
    return get_opc_home() / "artifacts" / "tool-results"


def persist_tool_result(
    content: Any,
    *,
    tool_name: str,
    task: Any | None = None,
    extension: str = "json",
) -> dict[str, Any]:
    """Persist full tool output and return path/size metadata."""
    if isinstance(content, str):
        text = content
        extension = extension or "txt"
    else:
        text = json.dumps(content, ensure_ascii=False, indent=2, default=str)
        extension = extension or "json"

    task_id = str(getattr(task, "id", "") or "").strip()
    session_id = str(getattr(task, "session_id", "") or getattr(task, "parent_session_id", "") or "").strip()
    bucket = _safe_segment(task_id or session_id or "global", "global")
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    digest = sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
    suffix = extension.lstrip(".") or "txt"
    path = _tool_results_root(task) / bucket / (
        f"{_safe_segment(tool_name, 'tool')}-{stamp}-{digest}.{suffix}"
    )
    workspace_root, output_root = workspace_roots_for_task(task)
    if task is not None and workspace_root:
        with SecureWorkspace(workspace_root, output_root or workspace_root) as workspace:
            target = workspace.resolve(
                str(path),
                use_output_root=False,
                allow_runtime_internal_read=True,
            )
            workspace.write_runtime_text(target, text, create_dirs=True)
            path = target.display_path
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return {
        "full_output_path": str(path),
        "original_size_chars": len(text),
    }


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


def budget_tool_output(
    output: dict[str, Any],
    *,
    tool_name: str,
    task: Any | None = None,
    max_chars: int,
    preview_chars: int | None = None,
    persist_large_results: bool = True,
    self_bounded_output: bool = False,
) -> dict[str, Any]:
    """Apply a recoverable registry-level budget to a tool output."""
    max_chars = max(1, int(max_chars or 1))
    try:
        serialized = json.dumps(output, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return output
    if len(serialized) <= max_chars:
        return output

    preview_limit = max(400, int(preview_chars or max_chars // 2))
    persisted: dict[str, Any] = {}
    if persist_large_results and not self_bounded_output:
        persisted = persist_tool_result(output, tool_name=tool_name, task=task, extension="json")

    meta = {
        "truncated": True,
        "omitted_chars": max(0, len(serialized) - max_chars),
        "original_size_chars": len(serialized),
        **persisted,
    }
    result_val = output.get("result")

    if isinstance(result_val, dict):
        preview_result = json.loads(json.dumps(result_val, ensure_ascii=False, default=str))
        for key in ("stdout", "stderr", "content", "rendered", "summary", "diff_preview"):
            value = preview_result.get(key)
            if isinstance(value, str):
                clip = clip_text(
                    value,
                    limit=preview_limit,
                    marker=f"{tool_name} {key} truncated",
                )
                preview_result[key] = clip.text
                if clip.truncated:
                    preview_result[f"{key}_truncated"] = True
                    preview_result[f"{key}_omitted_chars"] = clip.omitted_chars
        preview_result.update({key: value for key, value in meta.items() if value not in ("", None)})
        preview_output = {**output, "result": preview_result, **meta}
        if _json_size(preview_output) <= max_chars:
            return preview_output

    clip = clip_text(serialized, limit=preview_limit, marker=f"{tool_name} result truncated")
    return {
        "result": {
            "preview": clip.text,
            **{key: value for key, value in meta.items() if value not in ("", None)},
        },
        "success": output.get("success", True),
        **meta,
    }

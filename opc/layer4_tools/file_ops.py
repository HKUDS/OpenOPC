"""File system operation tools with compatibility aliases."""

from __future__ import annotations

import asyncio
import difflib
import fnmatch
import re
import shutil
from contextlib import closing
from pathlib import Path
from typing import Any

from opc.layer4_tools.output_budget import TextClip, clip_text
from opc.layer4_tools.registry import (
    COMPANY_EFFECT_NO_LOCAL_FS,
    COMPANY_EFFECT_STRUCTURED_PATHS,
    ToolDefinition,
)
from opc.layer4_tools.workspace_fs import (
    SecureWorkspace,
    WorkspaceBoundaryError,
    WorkspacePath,
)


_MAX_TEXT_CHARS = 12_000
_MAX_LIST_RESULTS = 500
_MAX_SEARCH_MATCH_CHARS = 500
_MAX_DIFF_CHARS = 16_000


class PatchApplyError(RuntimeError):
    """Raised when an apply_patch payload cannot be applied safely."""


def _assert_safe_path_pattern(pattern: str, *, label: str) -> None:
    candidate = Path(str(pattern or ""))
    if candidate.is_absolute() or ".." in candidate.parts:
        raise WorkspaceBoundaryError(
            f"{label} is outside the task workspace boundary: {pattern}"
        )


def _matches_path_pattern(path: str, pattern: str) -> bool:
    normalized_path = str(path).replace("\\", "/")
    normalized_pattern = str(pattern or "*").replace("\\", "/")
    if fnmatch.fnmatchcase(normalized_path, normalized_pattern):
        return True
    if fnmatch.fnmatchcase(Path(normalized_path).name, normalized_pattern):
        return True
    # pathlib.Path.rglob("**/*.py") also matches a root-level ``demo.py``.
    if normalized_pattern.startswith("**/"):
        return fnmatch.fnmatchcase(normalized_path, normalized_pattern[3:])
    return False


def _resolve_task_path(path: str, task: Any | None = None) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    base_dir = ""
    if task is not None:
        metadata = getattr(task, "metadata", {}) or {}
        candidate_roots = [
            str(metadata.get("output_root", "") or "").strip(),
            str(metadata.get("target_output_dir", "") or "").strip(),
            str(metadata.get("workspace_root", "") or "").strip(),
            str(metadata.get("comms_workspace_root", "") or "").strip(),
        ]
        for raw in candidate_roots:
            if not raw:
                continue
            resolved = Path(raw).expanduser()
            if resolved.exists():
                base_dir = str(resolved)
                break
            if not base_dir:
                base_dir = str(resolved)
    if base_dir:
        return Path(base_dir) / candidate
    return candidate


def _safe_read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _normalize_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _truncate_text(value: str, *, limit: int = _MAX_TEXT_CHARS) -> str:
    return clip_text(value, limit=limit, marker="truncated").text


def _truncate_match_entry(value: str, *, limit: int = _MAX_SEARCH_MATCH_CHARS) -> str:
    return clip_text(value, limit=limit, marker="match truncated", prefer_newline=False).text.replace("\n", " ")


def _build_diff_preview(old_text: str, new_text: str, path: str) -> TextClip:
    old_lines = _normalize_text(old_text).splitlines()
    new_lines = _normalize_text(new_text).splitlines()
    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"{path} (before)",
        tofile=f"{path} (after)",
        lineterm="",
    )
    return clip_text("\n".join(diff), limit=_MAX_DIFF_CHARS, marker="diff truncated")


def _diff_fields(old_text: str, new_text: str, path: str) -> dict[str, Any]:
    preview = _build_diff_preview(old_text, new_text, path)
    return {
        "diff_preview": preview.text,
        "diff_truncated": preview.truncated,
        "diff_omitted_chars": preview.omitted_chars,
        "diff_original_chars": preview.original_chars,
    }


def _render_file_slice(
    lines: list[str],
    *,
    offset: int,
    limit: int | None,
    include_line_numbers: bool,
) -> dict[str, Any]:
    start = max(0, int(offset or 0))
    line_limit = int(limit) if limit and int(limit) > 0 else None
    end = start + line_limit if line_limit is not None else len(lines)
    selected = lines[start:end]
    rendered_lines = [
        f"{start + idx + 1}: {line}" if include_line_numbers else line
        for idx, line in enumerate(selected)
    ]
    full_rendered = "".join(rendered_lines)
    if len(full_rendered) <= _MAX_TEXT_CHARS:
        returned_count = len(selected)
        return {
            "content": full_rendered,
            "truncated": False,
            "omitted_chars": 0,
            "returned_line_count": returned_count,
            "next_offset": start + returned_count if start + returned_count < len(lines) else None,
        }

    kept: list[str] = []
    used = 0
    for rendered in rendered_lines:
        if used + len(rendered) > _MAX_TEXT_CHARS:
            if not kept:
                first = clip_text(rendered, limit=_MAX_TEXT_CHARS, marker="file_read line truncated")
                kept.append(first.text)
            break
        kept.append(rendered)
        used += len(rendered)
    returned_count = max(1, len(kept)) if rendered_lines else 0
    content = "".join(kept)
    if not content.endswith("]"):
        omitted = max(0, len(full_rendered) - len(content))
        content = content.rstrip() + f"\n[file_read truncated: {omitted} chars omitted]"
    return {
        "content": content,
        "truncated": True,
        "omitted_chars": max(0, len(full_rendered) - len("".join(kept))),
        "returned_line_count": returned_count,
        "next_offset": start + returned_count if start + returned_count < len(lines) else None,
    }


def _iter_directory(root: Path, *, recursive: bool, max_depth: int) -> list[Path]:
    results: list[Path] = []

    def _walk(current: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = sorted(current.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
        except (FileNotFoundError, PermissionError):
            return
        for item in entries:
            results.append(item)
            if recursive and item.is_dir():
                _walk(item, depth + 1)

    _walk(root, 0)
    return results


def _apply_text_patch(current_text: str, patch_lines: list[str], path: str) -> str:
    updated = _normalize_text(current_text)
    current_pos = 0
    chunk: list[str] = []

    def _apply_chunk(buffer: list[str], working_text: str, cursor: int) -> tuple[str, int]:
        filtered = [line for line in buffer if line and line != "*** End of File"]
        if not filtered:
            return working_text, cursor
        search_lines = [line[1:] for line in filtered if line[:1] in {" ", "-"}]
        replace_lines = [line[1:] for line in filtered if line[:1] in {" ", "+"}]
        search_text = "\n".join(search_lines)
        replacement_text = "\n".join(replace_lines)
        if not search_text:
            raise PatchApplyError(f"Patch chunk for `{path}` is missing search context.")
        start = working_text.find(search_text, cursor)
        if start < 0:
            start = working_text.find(search_text)
        if start < 0:
            raise PatchApplyError(f"Unable to match patch chunk in `{path}`.")
        end = start + len(search_text)
        next_text = working_text[:start] + replacement_text + working_text[end:]
        return next_text, start + len(replacement_text)

    for line in patch_lines:
        if line.startswith("@@"):
            updated, current_pos = _apply_chunk(chunk, updated, current_pos)
            chunk = []
            continue
        if line.startswith((" ", "+", "-")) or line == "*** End of File":
            chunk.append(line)
            continue
        raise PatchApplyError(f"Unsupported patch line in `{path}`: {line}")
    updated, current_pos = _apply_chunk(chunk, updated, current_pos)
    _ = current_pos
    return updated


def _parse_patch_operations(patch: str) -> list[dict[str, Any]]:
    lines = patch.splitlines()
    if not lines or lines[0] != "*** Begin Patch":
        raise PatchApplyError("Patch must start with `*** Begin Patch`.")

    operations: list[dict[str, Any]] = []
    index = 1
    while index < len(lines):
        line = lines[index]
        if line == "*** End Patch":
            return operations
        if line.startswith("*** Add File: "):
            path = line[len("*** Add File: "):].strip()
            index += 1
            content_lines: list[str] = []
            while index < len(lines) and not lines[index].startswith("*** "):
                payload = lines[index]
                if not payload.startswith("+"):
                    raise PatchApplyError(f"Add File expects `+` lines only for `{path}`.")
                content_lines.append(payload[1:])
                index += 1
            operations.append({"kind": "add", "path": path, "content": "\n".join(content_lines)})
            continue
        if line.startswith("*** Delete File: "):
            path = line[len("*** Delete File: "):].strip()
            operations.append({"kind": "delete", "path": path})
            index += 1
            continue
        if line.startswith("*** Update File: "):
            path = line[len("*** Update File: "):].strip()
            index += 1
            move_to = None
            if index < len(lines) and lines[index].startswith("*** Move to: "):
                move_to = lines[index][len("*** Move to: "):].strip()
                index += 1
            patch_lines: list[str] = []
            while index < len(lines):
                candidate = lines[index]
                if candidate == "*** End Patch" or candidate.startswith("*** Add File: ") or candidate.startswith("*** Delete File: ") or candidate.startswith("*** Update File: "):
                    break
                patch_lines.append(candidate)
                index += 1
            operations.append({"kind": "update", "path": path, "move_to": move_to, "patch_lines": patch_lines})
            continue
        raise PatchApplyError(f"Unsupported patch operation: {line}")
    raise PatchApplyError("Patch is missing `*** End Patch`.")


FILE_MUTATION_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "apply_patch",
        "file_delete",
        "file_edit",
        "file_move",
        "file_write",
    }
)


def file_mutation_paths(
    tool_name: str,
    arguments: dict[str, Any] | None,
) -> tuple[str, ...]:
    """Return every path one structured file ToolCall may mutate.

    ``apply_patch`` deliberately reuses the executor's parser, so ownership
    preflight and the filesystem effect cannot disagree about a source,
    delete, or move target.  The delete/move spellings cover compatible tools
    if they are registered in a future tool surface.
    """

    name = str(tool_name or "").strip()
    if name not in FILE_MUTATION_TOOL_NAMES:
        return ()
    payload = dict(arguments or {})

    def _required(*keys: str) -> str:
        value = next(
            (
                str(payload.get(key, "") or "").strip()
                for key in keys
                if str(payload.get(key, "") or "").strip()
            ),
            "",
        )
        if not value:
            raise ValueError(
                f"Tool `{name}` is missing its filesystem mutation path."
            )
        return value

    if name in {"file_write", "file_edit", "file_delete"}:
        return (_required("path", "file_path"),)
    if name == "file_move":
        return tuple(
            dict.fromkeys(
                (
                    _required("path", "source", "src", "file_path"),
                    _required("destination", "dest", "target", "move_to"),
                )
            )
        )

    patch = str(payload.get("patch", "") or "")
    operations = _parse_patch_operations(patch)
    paths: list[str] = []
    for operation in operations:
        paths.append(str(operation.get("path", "") or "").strip())
        move_to = str(operation.get("move_to", "") or "").strip()
        if move_to:
            paths.append(move_to)
    if any(not path for path in paths):
        raise ValueError("apply_patch contains an empty filesystem path.")
    return tuple(dict.fromkeys(paths))


async def file_read(
    path: str,
    offset: int = 0,
    limit: int | None = None,
    include_line_numbers: bool = False,
    task: Any | None = None,
) -> dict[str, Any]:
    """Read file contents."""
    try:
        workspace = SecureWorkspace.for_task(task)
        if workspace is not None:
            with workspace:
                # Runtime-persisted large ToolResults live below
                # ``.opc-comms``.  Agents may read a path explicitly returned
                # by the runtime, but listing, searching, or mutating that
                # namespace remains forbidden.
                target = workspace.resolve(
                    path,
                    allow_runtime_internal_read=True,
                )
                text = workspace.read_text(target)
                rendered_path = str(target.display_path)
        else:
            p = _resolve_task_path(path, task)
            if not p.exists():
                return {"error": f"File not found: {path}", "success": False}
            if not p.is_file():
                return {"error": f"Not a file: {path}", "success": False}
            text = _safe_read_text(p)
            rendered_path = str(p.resolve())
        lines = text.splitlines(keepends=True)
        rendered = _render_file_slice(
            lines,
            offset=offset,
            limit=limit,
            include_line_numbers=include_line_numbers,
        )
        return {
            "content": rendered["content"],
            "total_lines": len(text.splitlines()),
            "returned_start_line": max(0, int(offset or 0)) + 1 if lines else 0,
            "returned_line_count": rendered["returned_line_count"],
            "next_offset": rendered["next_offset"],
            "truncated": rendered["truncated"],
            "omitted_chars": rendered["omitted_chars"],
            "path": rendered_path,
            "success": True,
        }
    except Exception as exc:
        return {"error": str(exc), "success": False}


async def file_write(path: str, content: str, create_dirs: bool = True, task: Any | None = None) -> dict[str, Any]:
    """Write content to a file."""
    try:
        workspace = SecureWorkspace.for_task(task)
        if workspace is not None:
            with workspace:
                target = workspace.resolve(path)
                before, created, bytes_written = workspace.write_text(
                    target,
                    content,
                    create_dirs=create_dirs,
                )
                rendered_path = str(target.display_path)
        else:
            p = _resolve_task_path(path, task)
            existed = p.exists()
            before = _safe_read_text(p) if existed and p.is_file() else ""
            if create_dirs:
                p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            created = not existed
            bytes_written = len(content.encode("utf-8"))
            rendered_path = str(p.resolve())
        return {
            "success": True,
            "path": rendered_path,
            "bytes_written": bytes_written,
            "created": created,
            **_diff_fields(before, content, rendered_path),
        }
    except Exception as exc:
        return {"error": str(exc), "success": False}


async def file_edit(
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    task: Any | None = None,
) -> dict[str, Any]:
    """Replace a specific string in a file."""
    try:
        replacement_count = 0

        def _replace(text: str) -> str:
            nonlocal replacement_count
            count = text.count(old_string)
            if count == 0:
                raise ValueError("old_string not found in file")
            if count > 1 and not replace_all:
                raise ValueError(
                    f"old_string found {count} times; add more context or use replace_all=true."
                )
            replacement_count = count if replace_all else 1
            if replace_all:
                return text.replace(old_string, new_string)
            return text.replace(old_string, new_string, 1)

        workspace = SecureWorkspace.for_task(task)
        if workspace is not None:
            with workspace:
                target = workspace.resolve(path)
                text, updated, _ = workspace.mutate_text(target, _replace)
                rendered_path = str(target.display_path)
        else:
            p = _resolve_task_path(path, task)
            if not p.exists():
                return {"error": f"File not found: {path}", "success": False}
            text = _safe_read_text(p)
            updated = _replace(text)
            p.write_text(updated, encoding="utf-8")
            rendered_path = str(p.resolve())
        return {
            "success": True,
            "path": rendered_path,
            "replacements": replacement_count,
            **_diff_fields(text, updated, rendered_path),
        }
    except Exception as exc:
        return {"error": str(exc), "success": False}


async def apply_patch(patch: str, task: Any | None = None) -> dict[str, Any]:
    """Apply an OpenAI-style patch document to one or more files."""
    try:
        operations = _parse_patch_operations(patch)
    except PatchApplyError as exc:
        return {"error": str(exc), "success": False}

    try:
        workspace = SecureWorkspace.for_task(task)
        if workspace is not None:
            with workspace:
                secured_operations: list[dict[str, Any]] = []
                for operation in operations:
                    secured = dict(operation)
                    secured["workspace_path"] = workspace.resolve(
                        str(operation["path"])
                    )
                    move_to = str(operation.get("move_to") or "").strip()
                    if move_to:
                        secured["workspace_target"] = workspace.resolve(move_to)
                    secured_operations.append(secured)
                return _apply_patch_in_workspace(workspace, secured_operations)
        return _apply_patch_unscoped(operations, task)
    except Exception as exc:
        return {"error": str(exc), "success": False}


def _apply_patch_in_workspace(
    workspace: SecureWorkspace,
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    changed: list[dict[str, Any]] = []
    for operation in operations:
        kind = operation["kind"]
        raw_path = str(operation["path"])
        path = operation["workspace_path"]
        rendered_path = str(path.display_path)
        if kind == "add":
            new_text = str(operation["content"])
            before, _, _ = workspace.write_text(
                path,
                new_text,
                create_dirs=True,
            )
            changed.append({
                "kind": "add",
                "path": rendered_path,
                **_diff_fields(before, new_text, rendered_path),
            })
            continue
        if kind == "delete":
            try:
                before = workspace.read_text(path)
            except FileNotFoundError as exc:
                raise PatchApplyError(
                    f"Cannot delete missing file `{raw_path}`."
                ) from exc
            workspace.unlink(path)
            changed.append({
                "kind": "delete",
                "path": rendered_path,
                **_diff_fields(before, "", rendered_path),
            })
            continue
        if kind == "update":
            def _update(before: str) -> str:
                return _apply_text_patch(
                    before,
                    list(operation.get("patch_lines", [])),
                    raw_path,
                )

            try:
                before, updated, _ = workspace.mutate_text(path, _update)
            except FileNotFoundError as exc:
                raise PatchApplyError(
                    f"Cannot update missing file `{raw_path}`."
                ) from exc
            target = operation.get("workspace_target") or path
            if target != path:
                workspace.rename(path, target)
            rendered_target = str(target.display_path)
            changed.append({
                "kind": "update",
                "path": rendered_target,
                "moved_from": rendered_path if target != path else "",
                **_diff_fields(before, updated, rendered_target),
            })
    return {
        "success": True,
        "changed_files": changed,
        "applied_operations": len(changed),
    }


def _apply_patch_unscoped(
    operations: list[dict[str, Any]],
    task: Any | None,
) -> dict[str, Any]:
    changed: list[dict[str, Any]] = []
    for operation in operations:
        kind = operation["kind"]
        raw_path = str(operation["path"])
        path = _resolve_task_path(raw_path, task)
        if kind == "add":
            path.parent.mkdir(parents=True, exist_ok=True)
            new_text = str(operation["content"])
            path.write_text(new_text, encoding="utf-8")
            changed.append({
                "kind": "add",
                "path": str(path.resolve()),
                **_diff_fields("", new_text, str(path)),
            })
            continue
        if kind == "delete":
            if not path.exists():
                raise PatchApplyError(f"Cannot delete missing file `{raw_path}`.")
            before = _safe_read_text(path) if path.is_file() else ""
            path.unlink()
            changed.append({
                "kind": "delete",
                "path": str(path.resolve()),
                **_diff_fields(before, "", str(path)),
            })
            continue
        if kind == "update":
            if not path.exists():
                raise PatchApplyError(f"Cannot update missing file `{raw_path}`.")
            before = _safe_read_text(path)
            updated = _apply_text_patch(
                before,
                list(operation.get("patch_lines", [])),
                raw_path,
            )
            target = _resolve_task_path(
                str(operation.get("move_to") or raw_path),
                task,
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            if target != path:
                path.unlink()
            target.write_text(updated, encoding="utf-8")
            changed.append({
                "kind": "update",
                "path": str(target.resolve()),
                "moved_from": str(path.resolve()) if target != path else "",
                **_diff_fields(before, updated, str(target)),
            })
    return {
        "success": True,
        "changed_files": changed,
        "applied_operations": len(changed),
    }


async def glob(
    pattern: str,
    path: str = ".",
    recursive: bool = True,
    include_dirs: bool = False,
    max_results: int = 200,
    task: Any | None = None,
) -> dict[str, Any]:
    """Return files matching a glob pattern."""
    try:
        workspace = SecureWorkspace.for_task(task)
        if workspace is not None:
            _assert_safe_path_pattern(pattern, label="Glob pattern")
            with workspace:
                target = workspace.resolve(path)
                entries: list[str] = []
                with closing(
                    workspace.iter_entries(
                        target,
                        recursive=recursive,
                    )
                ) as iterator:
                    for entry in iterator:
                        if entry.is_dir and not include_dirs:
                            continue
                        if not _matches_path_pattern(entry.relative_path, pattern):
                            continue
                        entries.append(entry.relative_path)
                        if len(entries) >= max_results:
                            break
                return {
                    "success": True,
                    "path": str(target.display_path),
                    "pattern": pattern,
                    "entries": entries,
                    "count": len(entries),
                }
        root = _resolve_task_path(path, task)
        if not root.exists():
            return {"error": f"Directory not found: {path}", "success": False}
        iterator = root.rglob(pattern) if recursive else root.glob(pattern)
        entries: list[str] = []
        for item in iterator:
            if item.is_dir() and not include_dirs:
                continue
            try:
                entries.append(str(item.relative_to(root)))
            except ValueError:
                entries.append(str(item))
            if len(entries) >= max_results:
                break
        return {
            "success": True,
            "path": str(root.resolve()),
            "pattern": pattern,
            "entries": entries,
            "count": len(entries),
        }
    except Exception as exc:
        return {"error": str(exc), "success": False}


async def grep(
    query: str,
    path: str = ".",
    file_glob: str = "*",
    max_results: int = 200,
    offset: int = 0,
    head_limit: int | None = None,
    output_mode: str = "content",
    case_sensitive: bool = False,
    context_before: int = 0,
    context_after: int = 0,
    task: Any | None = None,
) -> dict[str, Any]:
    """Search file contents for a regex or fixed-string query."""
    workspace = SecureWorkspace.for_task(task)
    if workspace is not None:
        try:
            _assert_safe_path_pattern(file_glob, label="File glob")
            with workspace:
                target = workspace.resolve(path)
                return _grep_workspace(
                    workspace,
                    target,
                    query=query,
                    file_glob=file_glob,
                    max_results=max_results,
                    offset=offset,
                    head_limit=head_limit,
                    output_mode=output_mode,
                    case_sensitive=case_sensitive,
                    context_before=context_before,
                    context_after=context_after,
                )
        except Exception as exc:
            return {"error": str(exc), "success": False}
    try:
        root = _resolve_task_path(path, task)
    except Exception as exc:
        return {"error": str(exc), "success": False}
    if not root.exists():
        return {"error": f"Directory not found: {path}", "success": False}
    applied_offset = max(0, int(offset or 0))
    effective_limit = max_results if head_limit is None else int(head_limit)

    def _apply_pagination(items: list[str]) -> tuple[list[str], bool, int | None]:
        if effective_limit == 0:
            paged = items[applied_offset:]
            return paged, False, None
        limit_value = max(0, effective_limit)
        paged = items[applied_offset:applied_offset + limit_value]
        truncated = applied_offset + limit_value < len(items)
        next_offset = applied_offset + limit_value if truncated else None
        return paged, truncated, next_offset

    def _format_output(all_matches: list[str]) -> dict[str, Any]:
        mode = str(output_mode or "content").strip() or "content"
        if mode not in {"content", "files_with_matches", "count"}:
            mode = "content"
        items = all_matches
        if mode == "files_with_matches":
            seen: set[str] = set()
            files: list[str] = []
            for entry in all_matches:
                file_part = entry.split(":", 1)[0]
                if file_part not in seen:
                    seen.add(file_part)
                    files.append(file_part)
            items = files
        elif mode == "count":
            counts: dict[str, int] = {}
            for entry in all_matches:
                file_part = entry.split(":", 1)[0]
                counts[file_part] = counts.get(file_part, 0) + 1
            items = [f"{name}: {count}" for name, count in sorted(counts.items())]
        paged, truncated, next_offset = _apply_pagination(items)
        clipped = [_truncate_match_entry(line) for line in paged]
        result: dict[str, Any] = {
            "success": True,
            "path": str(root.resolve()),
            "query": query,
            "output_mode": mode,
            "matches": clipped,
            "count": len(clipped),
            "total_count": len(items),
            "truncated": truncated,
            "next_offset": next_offset,
            "applied_offset": applied_offset,
        }
        if truncated and effective_limit != 0:
            result["applied_limit"] = max(0, effective_limit)
        if mode == "files_with_matches":
            result["files"] = clipped
            result["num_files"] = len(items)
        if mode == "count":
            result["counts"] = clipped
        return result

    rg = shutil.which("rg")
    if rg:
        args = [rg, "--no-heading", "--line-number", "--color", "never", "--glob", file_glob]
        if case_sensitive:
            args.append("--case-sensitive")
        else:
            args.append("--ignore-case")
        if context_before:
            args.extend(["-B", str(context_before)])
        if context_after:
            args.extend(["-A", str(context_after)])
        args.extend([query, str(root)])
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            output = stdout.decode("utf-8", errors="replace").splitlines()
            if proc.returncode not in (0, 1):
                return {"error": stderr.decode("utf-8", errors="replace"), "success": False}
            return _format_output(output)
        except Exception:
            pass

    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(query, flags=flags)
    matches: list[str] = []
    for candidate in root.rglob(file_glob):
        if not candidate.is_file():
            continue
        try:
            lines = _safe_read_text(candidate).splitlines()
        except Exception:
            continue
        for line_index, line in enumerate(lines):
            if not pattern.search(line):
                continue
            start = max(0, line_index - max(0, context_before))
            end = min(len(lines), line_index + max(0, context_after) + 1)
            context_lines = []
            for idx in range(start, end):
                context_lines.append(
                    _truncate_match_entry(f"{candidate.relative_to(root)}:{idx + 1}:{lines[idx]}")
                )
            matches.extend(context_lines)
    return _format_output(matches)


def _grep_workspace(
    workspace: SecureWorkspace,
    root: WorkspacePath,
    *,
    query: str,
    file_glob: str,
    max_results: int,
    offset: int,
    head_limit: int | None,
    output_mode: str,
    case_sensitive: bool,
    context_before: int,
    context_after: int,
) -> dict[str, Any]:
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(query, flags=flags)
    all_matches: list[str] = []
    with closing(workspace.iter_entries(root, recursive=True)) as iterator:
        for entry in iterator:
            if entry.is_dir or not _matches_path_pattern(entry.relative_path, file_glob):
                continue
            candidate = workspace.resolve(
                str(Path(*root.parts, *entry.parts)),
                use_output_root=False,
            )
            try:
                lines = workspace.read_text(candidate).splitlines()
            except (FileNotFoundError, WorkspaceBoundaryError):
                continue
            for line_index, line in enumerate(lines):
                if not pattern.search(line):
                    continue
                start = max(0, line_index - max(0, context_before))
                end = min(len(lines), line_index + max(0, context_after) + 1)
                for idx in range(start, end):
                    all_matches.append(
                        _truncate_match_entry(
                            f"{entry.relative_path}:{idx + 1}:{lines[idx]}"
                        )
                    )

    mode = str(output_mode or "content").strip() or "content"
    if mode not in {"content", "files_with_matches", "count"}:
        mode = "content"
    items = all_matches
    if mode == "files_with_matches":
        items = list(dict.fromkeys(entry.split(":", 1)[0] for entry in all_matches))
    elif mode == "count":
        counts: dict[str, int] = {}
        for entry in all_matches:
            file_part = entry.split(":", 1)[0]
            counts[file_part] = counts.get(file_part, 0) + 1
        items = [f"{name}: {count}" for name, count in sorted(counts.items())]

    applied_offset = max(0, int(offset or 0))
    effective_limit = max_results if head_limit is None else int(head_limit)
    if effective_limit == 0:
        paged = items[applied_offset:]
        truncated = False
        next_offset = None
    else:
        limit_value = max(0, effective_limit)
        paged = items[applied_offset:applied_offset + limit_value]
        truncated = applied_offset + limit_value < len(items)
        next_offset = applied_offset + limit_value if truncated else None
    result: dict[str, Any] = {
        "success": True,
        "path": str(root.display_path),
        "query": query,
        "output_mode": mode,
        "matches": paged,
        "count": len(paged),
        "total_count": len(items),
        "truncated": truncated,
        "next_offset": next_offset,
        "applied_offset": applied_offset,
    }
    if truncated and effective_limit != 0:
        result["applied_limit"] = max(0, effective_limit)
    if mode == "files_with_matches":
        result["files"] = paged
        result["num_files"] = len(items)
    if mode == "count":
        result["counts"] = paged
    return result


async def file_search(
    pattern: str,
    directory: str = ".",
    file_glob: str = "*",
    max_results: int = 50,
    offset: int = 0,
    head_limit: int | None = None,
    task: Any | None = None,
) -> dict[str, Any]:
    """Compatibility wrapper for grep."""
    result = await grep(
        query=pattern,
        path=directory,
        file_glob=file_glob,
        max_results=max_results,
        offset=offset,
        head_limit=head_limit,
        task=task,
    )
    if result.get("success"):
        result["matches"] = list(result.get("matches", []))
    return result


async def list_dir(path: str = ".", recursive: bool = False, max_depth: int = 3, task: Any | None = None) -> dict[str, Any]:
    """List directory contents."""
    try:
        workspace = SecureWorkspace.for_task(task)
        if workspace is not None:
            with workspace:
                target = workspace.resolve(path)
                entries: list[str] = []
                items: list[dict[str, Any]] = []
                with closing(
                    workspace.iter_entries(
                        target,
                        recursive=recursive,
                        max_depth=max_depth,
                    )
                ) as iterator:
                    for entry in iterator:
                        rendered = entry.relative_path + ("/" if entry.is_dir else "")
                        entries.append(rendered)
                        items.append({
                            "path": rendered,
                            "is_dir": entry.is_dir,
                            "size": entry.size,
                        })
                        if len(items) >= _MAX_LIST_RESULTS:
                            break
                return {
                    "success": True,
                    "path": str(target.display_path),
                    "entries": entries,
                    "items": items,
                    "total": len(items),
                }
        root = _resolve_task_path(path, task)
    except Exception as exc:
        return {"error": str(exc), "success": False}
    if not root.exists():
        return {"error": f"Directory not found: {path}", "success": False}
    if not root.is_dir():
        return {"error": f"Not a directory: {path}", "success": False}

    entries: list[str] = []
    items: list[dict[str, Any]] = []
    for item in _iter_directory(
        root,
        recursive=recursive,
        max_depth=max_depth,
    ):
        try:
            relative = str(item.relative_to(root))
        except ValueError:
            relative = str(item)
        entry_path = relative + ("/" if item.is_dir() else "")
        entries.append(entry_path)
        items.append({
            "path": relative + ("/" if item.is_dir() else ""),
            "is_dir": item.is_dir(),
            "size": item.stat().st_size if item.is_file() else 0,
        })
        if len(items) >= _MAX_LIST_RESULTS:
            break

    return {
        "success": True,
        "path": str(root.resolve()),
        "entries": entries,
        "items": items,
        "total": len(items),
    }


def create_file_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="file_read",
            description="Read the contents of a file. Supports line offset, line limit, and optional line numbers.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file"},
                    "offset": {"type": "integer", "description": "Line offset (0-based)", "default": 0},
                    "limit": {"type": "integer", "description": "Maximum lines to read"},
                    "include_line_numbers": {"type": "boolean", "description": "Prefix output with line numbers", "default": False},
                },
                "required": ["path"],
            },
            func=file_read,
            category="file",
            concurrency_safe=True,
            read_only=True,
            self_bounded_output=True,
            max_result_chars=80_000,
            company_effect_kind=COMPANY_EFFECT_NO_LOCAL_FS,
            permission_effects=["local_read"],
        ),
        ToolDefinition(
            name="file_write",
            description="Write content to a file and return a diff preview against the previous content.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file"},
                    "content": {"type": "string", "description": "Content to write"},
                    "create_dirs": {"type": "boolean", "description": "Create parent directories if needed", "default": True},
                },
                "required": ["path", "content"],
            },
            func=file_write,
            category="file",
            concurrency_safe=False,
            read_only=False,
            company_effect_kind=COMPANY_EFFECT_STRUCTURED_PATHS,
            permission_effects=["workspace_write"],
        ),
        ToolDefinition(
            name="file_edit",
            description="Replace a specific string in a file and return a diff preview.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file"},
                    "old_string": {"type": "string", "description": "Exact string to find"},
                    "new_string": {"type": "string", "description": "Replacement string"},
                    "replace_all": {"type": "boolean", "description": "Replace all occurrences instead of requiring uniqueness", "default": False},
                },
                "required": ["path", "old_string", "new_string"],
            },
            func=file_edit,
            category="file",
            concurrency_safe=False,
            read_only=False,
            company_effect_kind=COMPANY_EFFECT_STRUCTURED_PATHS,
            permission_effects=["workspace_write"],
        ),
        ToolDefinition(
            name="apply_patch",
            description="Apply an OpenAI-style patch document to one or more files.",
            parameters={
                "type": "object",
                "properties": {
                    "patch": {"type": "string", "description": "Patch document beginning with `*** Begin Patch`"},
                },
                "required": ["patch"],
            },
            func=apply_patch,
            category="file",
            concurrency_safe=False,
            read_only=False,
            company_effect_kind=COMPANY_EFFECT_STRUCTURED_PATHS,
            permission_effects=["workspace_write"],
        ),
        ToolDefinition(
            name="grep",
            description="Search file contents using ripgrep when available, with a Python fallback.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Regex or fixed-string query"},
                    "path": {"type": "string", "description": "Directory to search", "default": "."},
                    "file_glob": {"type": "string", "description": "File glob pattern", "default": "*"},
                    "max_results": {"type": "integer", "description": "Maximum returned matches", "default": 200},
                    "offset": {"type": "integer", "description": "Skip this many results before returning matches", "default": 0},
                    "head_limit": {"type": "integer", "description": "Maximum returned entries; 0 means unlimited", "default": 200},
                    "output_mode": {
                        "type": "string",
                        "description": "Return matching lines (`content`), unique files (`files_with_matches`), or per-file counts (`count`)",
                        "default": "content",
                    },
                    "case_sensitive": {"type": "boolean", "description": "Use case-sensitive matching", "default": False},
                    "context_before": {"type": "integer", "description": "Context lines before each hit", "default": 0},
                    "context_after": {"type": "integer", "description": "Context lines after each hit", "default": 0},
                },
                "required": ["query"],
            },
            func=grep,
            category="file",
            concurrency_safe=True,
            read_only=True,
            self_bounded_output=True,
            max_result_chars=80_000,
            company_effect_kind=COMPANY_EFFECT_NO_LOCAL_FS,
            permission_effects=["local_read"],
        ),
        ToolDefinition(
            name="glob",
            description="Return files matching a glob pattern.",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern to match"},
                    "path": {"type": "string", "description": "Directory to scan", "default": "."},
                    "recursive": {"type": "boolean", "description": "Search recursively", "default": True},
                    "include_dirs": {"type": "boolean", "description": "Include directories in the result set", "default": False},
                    "max_results": {"type": "integer", "description": "Maximum entries to return", "default": 200},
                },
                "required": ["pattern"],
            },
            func=glob,
            category="file",
            concurrency_safe=True,
            read_only=True,
            company_effect_kind=COMPANY_EFFECT_NO_LOCAL_FS,
            permission_effects=["local_read"],
        ),
        ToolDefinition(
            name="file_search",
            description="Compatibility alias for `grep`.",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search"},
                    "directory": {"type": "string", "description": "Directory to search in", "default": "."},
                    "file_glob": {"type": "string", "description": "File glob pattern", "default": "*"},
                    "max_results": {"type": "integer", "description": "Maximum results", "default": 50},
                    "offset": {"type": "integer", "description": "Skip this many results before returning matches", "default": 0},
                    "head_limit": {"type": "integer", "description": "Maximum returned entries; 0 means unlimited"},
                },
                "required": ["pattern"],
            },
            func=file_search,
            category="file",
            concurrency_safe=True,
            read_only=True,
            company_effect_kind=COMPANY_EFFECT_NO_LOCAL_FS,
            permission_effects=["local_read"],
        ),
        ToolDefinition(
            name="list_dir",
            description="List directory contents. Optionally recurse up to a maximum depth.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path", "default": "."},
                    "recursive": {"type": "boolean", "description": "List recursively", "default": False},
                    "max_depth": {"type": "integer", "description": "Maximum recursion depth", "default": 3},
                },
                "required": [],
            },
            func=list_dir,
            category="file",
            concurrency_safe=True,
            read_only=True,
            company_effect_kind=COMPANY_EFFECT_NO_LOCAL_FS,
            permission_effects=["local_read"],
        ),
    ]

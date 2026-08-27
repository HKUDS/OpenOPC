"""Shared policy helpers for data acquisition work-item projections."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from opc.layer2_organization.work_item_identity import projection_id_for_task


DATA_ACQUISITION_PROJECTION_ID = "data_acquisition"
ACQUISITION_SPECIALIST_ROLE_ID = "acquisition_specialist"
ACQUISITION_SHELL_PREFIXES = {"curl", "wget", "yt-dlp", "aria2c", "ffmpeg"}
MEDIA_BINARY_ASSET_KEYWORDS = (
    "video",
    "trailer",
    "footage",
    "clip",
    "素材",
    "片段",
    "audio",
    "music",
    "subtitle",
    "srt",
    "bilibili",
    "youtube",
    "mp4",
    "wav",
)
DEFAULT_SOURCE_CANDIDATES_RELATIVE_PATH = "work/source_candidates.json"
DEFAULT_DOWNLOAD_MANIFEST_RELATIVE_PATH = "work/download_manifest.json"
DEFAULT_ACQUISITION_EXECUTION_RECORD_RELATIVE_PATH = "deliverables/acquisition_execution_record.md"
MEDIA_ASSET_SUBDIRS = ("trailers", "audio", "subtitles")


def task_projection_id(task: Any | None) -> str:
    if task is None:
        return ""
    return projection_id_for_task(task).lower()


def task_role_id(task: Any | None) -> str:
    if task is None:
        return ""
    metadata = getattr(task, "metadata", {}) or {}
    assigned = str(getattr(task, "assigned_to", "") or "").strip().lower()
    if assigned:
        return assigned
    # TODO(role-identity): route through a central work-item role accessor
    # when assigned_to is empty (e.g. provisioning subtasks).
    return str(metadata.get("work_item_role_id", "") or "").strip().lower()


def is_acquisition_specialist_projection(
    *,
    task: Any | None = None,
    projection_id: str = "",
    role_id: str = "",
) -> bool:
    resolved_projection = str(projection_id or task_projection_id(task) or "").strip().lower()
    resolved_role = str(role_id or task_role_id(task) or "").strip().lower()
    return resolved_projection == DATA_ACQUISITION_PROJECTION_ID and resolved_role == ACQUISITION_SPECIALIST_ROLE_ID


def workspace_root_for_task(task: Any | None) -> Path | None:
    if task is None:
        return None
    metadata = getattr(task, "metadata", {}) or {}
    manifest = dict(metadata.get("workspace_manifest", {}) or {})
    root = str(manifest.get("root_path", "") or metadata.get("target_output_dir", "") or "").strip()
    if not root:
        return None
    try:
        return Path(root).resolve()
    except Exception:
        return None


def reserved_path_for_task(task: Any | None, key: str) -> Path | None:
    if task is None:
        return None
    manifest = dict(getattr(task, "metadata", {}).get("workspace_manifest", {}) or {})
    reserved = dict(manifest.get("reserved_paths", {}) or {})
    raw = str(reserved.get(key, "") or "").strip()
    if not raw:
        return None
    try:
        return Path(raw).resolve()
    except Exception:
        return None


def default_source_candidates_path(task: Any | None) -> str:
    work_dir = reserved_path_for_task(task, "work")
    if work_dir is not None:
        return str((work_dir / "source_candidates.json").resolve())
    root = workspace_root_for_task(task)
    if root is not None:
        return str((root / DEFAULT_SOURCE_CANDIDATES_RELATIVE_PATH).resolve())
    return DEFAULT_SOURCE_CANDIDATES_RELATIVE_PATH


def default_download_manifest_path(task: Any | None) -> str:
    work_dir = reserved_path_for_task(task, "work")
    if work_dir is not None:
        return str((work_dir / "download_manifest.json").resolve())
    root = workspace_root_for_task(task)
    if root is not None:
        return str((root / DEFAULT_DOWNLOAD_MANIFEST_RELATIVE_PATH).resolve())
    return DEFAULT_DOWNLOAD_MANIFEST_RELATIVE_PATH


def default_execution_record_path(task: Any | None) -> str:
    deliverables_dir = reserved_path_for_task(task, "deliverables")
    if deliverables_dir is not None:
        return str((deliverables_dir / "acquisition_execution_record.md").resolve())
    root = workspace_root_for_task(task)
    if root is not None:
        return str((root / DEFAULT_ACQUISITION_EXECUTION_RECORD_RELATIVE_PATH).resolve())
    return DEFAULT_ACQUISITION_EXECUTION_RECORD_RELATIVE_PATH


def _normalize_item_text(value: Any) -> str:
    if isinstance(value, dict):
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except TypeError:
            return str(value).strip()
    if isinstance(value, (list, tuple, set)):
        return " ".join(_normalize_item_text(item) for item in value if _normalize_item_text(item))
    return str(value or "").strip()


def requires_binary_asset_acquisition(task: Any | None, report: dict[str, Any] | None = None) -> bool:
    texts: list[str] = []
    if task is not None:
        metadata = getattr(task, "metadata", {}) or {}
        texts.extend([
            str(getattr(task, "title", "") or ""),
            str(getattr(task, "description", "") or ""),
            str(metadata.get("original_message", "") or ""),
        ])
    report_payload = dict(report or {})
    for key in ("required_inputs", "present_inputs", "missing_inputs", "attempted_sources", "notes", "blocked_reasons"):
        raw = report_payload.get(key, [])
        if isinstance(raw, list):
            texts.extend(_normalize_item_text(item) for item in raw)
        elif raw not in (None, "", [], {}):
            texts.append(_normalize_item_text(raw))
    combined = "\n".join(text.lower() for text in texts if text).strip()
    if not combined:
        return False
    return any(keyword in combined for keyword in MEDIA_BINARY_ASSET_KEYWORDS)


def load_json_file(path: str) -> Any:
    raw = str(path or "").strip()
    if not raw:
        return None
    try:
        return json.loads(Path(raw).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _manifest_entries(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("entries", "items", "downloads", "download_manifest"):
            nested = value.get(key)
            if isinstance(nested, list):
                return [dict(item) for item in nested if isinstance(item, dict)]
    return []


def load_download_manifest_entries(path: str) -> list[dict[str, Any]]:
    return _manifest_entries(load_json_file(path))


def _path_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def has_downloaded_binary_asset(
    *,
    task: Any | None,
    report: dict[str, Any],
    download_manifest_path: str,
    designated_input_dir: str,
) -> bool:
    manifest_entries = load_download_manifest_entries(download_manifest_path)
    if not manifest_entries:
        return False
    input_root = None
    if designated_input_dir:
        try:
            input_root = Path(designated_input_dir).resolve()
        except Exception:
            input_root = None
    for entry in manifest_entries:
        status = str(entry.get("status", "") or "").strip().lower()
        if status != "downloaded":
            continue
        local_path = str(entry.get("local_path", "") or "").strip()
        if not local_path:
            continue
        try:
            candidate = Path(local_path).resolve()
        except Exception:
            continue
        media_kind = str(entry.get("media_kind", "") or "").strip().lower()
        if input_root is not None and _path_within(candidate, input_root):
            relative_parts = candidate.relative_to(input_root).parts
            if relative_parts and relative_parts[0] in MEDIA_ASSET_SUBDIRS:
                return True
            if media_kind in {"video", "audio", "subtitle"}:
                return True
        elif workspace_root_for_task(task) is not None and _path_within(candidate, workspace_root_for_task(task) or Path(".")):
            if media_kind in {"video", "audio", "subtitle"}:
                return True
    return False

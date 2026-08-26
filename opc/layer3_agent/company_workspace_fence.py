"""Fail-closed filesystem attestation for company-owned external execution."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opc.core.models import Task


_IGNORED_TOP_LEVEL = frozenset({".git", ".agent_teams", ".jiuwenswarm", ".opc-attachments"})
_MAX_FILES = 50_000
_MAX_HASH_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class FileAttestation:
    kind: str
    size: int
    mtime_ns: int
    sha256: str = ""
    link_target: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "sha256": self.sha256,
            "link_target": self.link_target,
        }


@dataclass
class CompanyWorkspaceSnapshot:
    root: Path
    files: dict[str, FileAttestation]
    ignored_roots: list[str]


class CompanyWorkspaceFenceError(RuntimeError):
    pass


def company_external_fence_enabled(task: Task) -> bool:
    metadata = dict(task.metadata or {})
    return bool(
        task.assigned_external_agent
        and metadata.get("external_company_execution_allowed") is True
        and str(metadata.get("external_company_execution_fence") or "").strip()
        == "validated_workspace"
    )


def capture_company_workspace(workspace_path: str | Path) -> CompanyWorkspaceSnapshot:
    root = Path(workspace_path).expanduser().resolve()
    if not root.is_dir():
        raise CompanyWorkspaceFenceError(f"company external workspace is not a directory: {root}")
    files: dict[str, FileAttestation] = {}
    count = 0
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        relative_dir = current_path.relative_to(root)
        if relative_dir == Path("."):
            dirnames[:] = [name for name in dirnames if name not in _IGNORED_TOP_LEVEL]
        for name in sorted([*dirnames, *filenames]):
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if relative.split("/", 1)[0] in _IGNORED_TOP_LEVEL:
                continue
            count += 1
            if count > _MAX_FILES:
                raise CompanyWorkspaceFenceError(
                    f"company external workspace exceeds {_MAX_FILES} attestable paths"
                )
            try:
                stat = path.lstat()
            except OSError as exc:
                raise CompanyWorkspaceFenceError(f"cannot attest {relative}: {exc}") from exc
            if path.is_symlink():
                try:
                    path.resolve(strict=False).relative_to(root)
                except (OSError, ValueError) as exc:
                    raise CompanyWorkspaceFenceError(
                        f"company external workspace contains an escaping symlink: "
                        f"{relative} -> {os.readlink(path)}"
                    ) from exc
                files[relative] = FileAttestation(
                    kind="symlink",
                    size=int(stat.st_size),
                    mtime_ns=int(stat.st_mtime_ns),
                    link_target=os.readlink(path),
                )
            elif path.is_dir():
                files[relative] = FileAttestation(
                    kind="directory",
                    size=0,
                    mtime_ns=int(stat.st_mtime_ns),
                )
            elif path.is_file():
                digest = ""
                if stat.st_size <= _MAX_HASH_BYTES:
                    hasher = hashlib.sha256()
                    try:
                        with path.open("rb") as handle:
                            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                                hasher.update(chunk)
                    except OSError as exc:
                        raise CompanyWorkspaceFenceError(f"cannot hash {relative}: {exc}") from exc
                    digest = hasher.hexdigest()
                files[relative] = FileAttestation(
                    kind="file",
                    size=int(stat.st_size),
                    mtime_ns=int(stat.st_mtime_ns),
                    sha256=digest,
                )
            else:
                raise CompanyWorkspaceFenceError(
                    f"unsupported filesystem object in company workspace: {relative}"
                )
    return CompanyWorkspaceSnapshot(
        root=root,
        files=files,
        ignored_roots=sorted(_IGNORED_TOP_LEVEL),
    )


def validate_company_workspace(
    before: CompanyWorkspaceSnapshot,
    workspace_path: str | Path,
) -> dict[str, Any]:
    after = capture_company_workspace(workspace_path)
    if after.root != before.root:
        raise CompanyWorkspaceFenceError(
            f"company external workspace identity changed: {before.root} -> {after.root}"
        )
    changed: list[str] = []
    created: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []
    for relative in sorted(set(before.files) | set(after.files)):
        prior = before.files.get(relative)
        current = after.files.get(relative)
        if prior == current:
            continue
        changed.append(relative)
        if prior is None:
            created.append(relative)
        elif current is None:
            deleted.append(relative)
        else:
            modified.append(relative)
        if current is not None and current.kind == "symlink":
            link_path = after.root / relative
            try:
                resolved = link_path.resolve(strict=False)
                resolved.relative_to(after.root)
            except (OSError, ValueError) as exc:
                raise CompanyWorkspaceFenceError(
                    f"company external agent created an escaping symlink: {relative} -> {current.link_target}"
                ) from exc

    manifest = []
    for relative in changed:
        current = after.files.get(relative)
        if current is None or current.kind == "directory":
            continue
        manifest.append(
            {
                "path": relative,
                **current.to_dict(),
            }
        )
    return {
        "fence": "validated_workspace",
        "workspace": str(after.root),
        "changed_paths": changed,
        "created_paths": created,
        "modified_paths": modified,
        "deleted_paths": deleted,
        "artifact_manifest": manifest,
        "ignored_provider_roots": list(after.ignored_roots),
        "formal_change_count": len(changed),
        "validated": True,
    }

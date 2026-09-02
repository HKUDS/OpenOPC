"""Fail-closed filesystem attestation for company-owned external execution."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opc.core.models import Task
from opc.layer4_tools.workspace_fs import SecureWorkspace, WorkspaceBoundaryError


_IGNORED_TOP_LEVEL = frozenset(
    {".git", ".agent_teams", ".jiuwenswarm", ".opc-attachments"}
)
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
    lexical_root = Path(
        os.path.abspath(os.path.normpath(Path(workspace_path).expanduser()))
    )
    try:
        root_info = lexical_root.lstat()
    except OSError as exc:
        raise CompanyWorkspaceFenceError(
            f"cannot attest company external workspace root: {lexical_root}: {exc}"
        ) from exc
    if _link_or_reparse(root_info, lexical_root):
        raise CompanyWorkspaceFenceError(
            f"company external workspace root must not be a link or reparse point: {lexical_root}"
        )
    root = lexical_root
    if not stat.S_ISDIR(root_info.st_mode):
        raise CompanyWorkspaceFenceError(
            f"company external workspace is not a directory: {root}"
        )
    files: dict[str, FileAttestation] = {}
    count = 0
    try:
        with SecureWorkspace(str(root), str(root)) as secure:
            for current, dirnames, filenames in os.walk(
                root, topdown=True, followlinks=False
            ):
                current_path = Path(current)
                relative_dir = current_path.relative_to(root)
                if relative_dir == Path("."):
                    dirnames[:] = [
                        name for name in dirnames if name not in _IGNORED_TOP_LEVEL
                    ]
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
                        path_info = path.lstat()
                    except OSError as exc:
                        raise CompanyWorkspaceFenceError(
                            f"cannot attest {relative}: {exc}"
                        ) from exc
                    if _link_or_reparse(path_info, path):
                        if os.name == "nt":
                            raise CompanyWorkspaceFenceError(
                                "company external workspace contains a Windows reparse point: "
                                f"{relative}"
                            )
                        try:
                            link_target = os.readlink(path)
                            path.resolve(strict=False).relative_to(root)
                        except (OSError, ValueError) as exc:
                            raise CompanyWorkspaceFenceError(
                                "company external workspace contains an escaping symlink: "
                                f"{relative} -> {os.readlink(path)}"
                            ) from exc
                        files[relative] = FileAttestation(
                            kind="symlink",
                            size=int(path_info.st_size),
                            mtime_ns=int(path_info.st_mtime_ns),
                            link_target=link_target,
                        )
                    elif stat.S_ISDIR(path_info.st_mode):
                        files[relative] = FileAttestation(
                            kind="directory",
                            size=0,
                            mtime_ns=int(path_info.st_mtime_ns),
                        )
                    elif stat.S_ISREG(path_info.st_mode):
                        if int(path_info.st_nlink) != 1:
                            raise CompanyWorkspaceFenceError(
                                "company external workspace contains a multiply-linked file: "
                                f"{relative}"
                            )
                        digest = ""
                        if path_info.st_size <= _MAX_HASH_BYTES:
                            try:
                                target = secure.resolve(relative, use_output_root=False)
                                digest = hashlib.sha256(
                                    secure.read_bytes(target)
                                ).hexdigest()
                            except (OSError, WorkspaceBoundaryError) as exc:
                                raise CompanyWorkspaceFenceError(
                                    f"cannot securely hash {relative}: {exc}"
                                ) from exc
                        files[relative] = FileAttestation(
                            kind="file",
                            size=int(path_info.st_size),
                            mtime_ns=int(path_info.st_mtime_ns),
                            sha256=digest,
                        )
                    else:
                        raise CompanyWorkspaceFenceError(
                            f"unsupported filesystem object in company workspace: {relative}"
                        )
    except WorkspaceBoundaryError as exc:
        raise CompanyWorkspaceFenceError(str(exc)) from exc
    return CompanyWorkspaceSnapshot(
        root=root,
        files=files,
        ignored_roots=sorted(_IGNORED_TOP_LEVEL),
    )


def _link_or_reparse(info: os.stat_result, path: Path) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
    if attributes and reparse_flag and attributes & reparse_flag:
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(callable(is_junction) and is_junction())


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

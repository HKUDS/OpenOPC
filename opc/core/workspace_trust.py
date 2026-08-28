"""User-owned trust decisions for project-local OpenOPC configuration.

Project configuration is intentionally powerful: it can select executables,
network endpoints, and credential sources.  Trust records therefore live
outside ``OPC_HOME`` so a repository cannot grant trust to itself by
committing files below ``.opc``.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any


_TRUST_STORE_VERSION = 2
_AUTHORITY_SOURCE_FILES = (
    "system_config.yaml",
    "llm_config.yaml",
    "agent_config.yaml",
    "channel_config.yaml",
)
_AUTHORITY_CONFIG_FIELDS = (
    "system",
    "llm",
    "agents",
    "channels",
    "autonomy",
    "capabilities",
)


def canonical_workspace(path: str | Path) -> Path:
    """Return a stable workspace identity across relative and symlink paths."""

    return Path(path).expanduser().resolve(strict=False)


def user_config_root() -> Path:
    """Return an OS-appropriate, user-owned config root independent of OPC_HOME."""

    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base.expanduser().resolve(strict=False) / "openopc"


def default_trust_store_path() -> Path:
    return user_config_root() / "trusted_workspaces.json"


def authority_source_fingerprint(config_dir: str | Path) -> str:
    """Hash project-controlled files that can grant runtime authority.

    Symlinks are represented by their link target without following it.  This
    keeps the pre-parse gate bounded to repository metadata; the normalized
    effective-config check below catches changes in a linked YAML target before
    any engine sink is reached.
    """

    root = Path(config_dir).expanduser()
    digest = hashlib.sha256()
    digest.update(b"openopc-workspace-authority-source-v1\0")
    for relative_name in _AUTHORITY_SOURCE_FILES:
        path = root / relative_name
        digest.update(relative_name.encode("utf-8"))
        digest.update(b"\0")
        try:
            metadata = path.lstat()
        except OSError as exc:
            digest.update(f"missing:{type(exc).__name__}".encode("ascii"))
            digest.update(b"\0")
            continue

        if stat.S_ISLNK(metadata.st_mode):
            try:
                target = os.readlink(path)
            except OSError as exc:
                target = f"unreadable:{type(exc).__name__}"
            digest.update(b"symlink\0")
            digest.update(os.fsencode(target))
            digest.update(b"\0")
            continue
        if not stat.S_ISREG(metadata.st_mode):
            digest.update(f"nonregular:{stat.S_IFMT(metadata.st_mode)}".encode("ascii"))
            digest.update(b"\0")
            continue

        digest.update(b"regular\0")
        try:
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
        except OSError as exc:
            digest.update(f"unreadable:{type(exc).__name__}".encode("ascii"))
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def effective_authority_fingerprint(config: Any) -> str:
    """Hash normalized configuration fields that control privileged sinks."""

    authority: dict[str, Any] = {}
    for field_name in _AUTHORITY_CONFIG_FIELDS:
        value = getattr(config, field_name, None)
        if hasattr(value, "model_dump"):
            authority[field_name] = value.model_dump(mode="json")
        else:
            authority[field_name] = value
    serialized = json.dumps(
        authority,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(b"openopc-effective-authority-v1\0" + serialized)
    return f"sha256:{digest.hexdigest()}"


def project_workspace_for_config(
    config_dir: str | Path,
    *,
    active_project_root: str | Path | None = None,
) -> Path | None:
    """Identify ``<workspace>/.opc/config`` without classifying arbitrary homes.

    A normal project is recognized by ``pyproject.toml``.  The active project
    root fallback preserves OpenOPC's existing behavior in directories that do
    not contain one: ``_find_project_root`` returns the current directory there.
    """

    lexical = Path(os.path.abspath(os.fspath(Path(config_dir).expanduser())))
    if lexical.name != "config" or lexical.parent.name != ".opc":
        return None

    workspace = canonical_workspace(lexical.parent.parent)
    if (workspace / "pyproject.toml").is_file():
        return workspace

    if active_project_root is None:
        return None
    active = canonical_workspace(active_project_root)
    expected = (active / ".opc" / "config").resolve(strict=False)
    if lexical.resolve(strict=False) == expected:
        return active
    return None


def workspace_from_user_path(path: str | Path) -> Path:
    """Resolve a CLI path to the nearest workspace containing ``.opc/config``."""

    candidate = Path(path).expanduser()
    if candidate.name == "config" and candidate.parent.name == ".opc":
        workspace = candidate.parent.parent
        if candidate.is_dir():
            return canonical_workspace(workspace)
    elif candidate.name == ".opc":
        if (candidate / "config").is_dir():
            return canonical_workspace(candidate.parent)

    start = candidate if candidate.is_dir() else candidate.parent
    start = canonical_workspace(start)
    for parent in (start, *start.parents):
        if (parent / ".opc" / "config").is_dir():
            return canonical_workspace(parent)
    raise ValueError(f"No .opc/config directory found for {path}")


class WorkspaceTrustRequired(RuntimeError):
    """Raised before project-controlled configuration can be loaded."""

    def __init__(
        self,
        workspace: Path,
        config_dir: Path,
        *,
        reason: str = "untrusted",
        current_fingerprint: str = "",
    ) -> None:
        self.workspace = workspace
        self.config_dir = config_dir
        self.reason = reason
        self.current_fingerprint = current_fingerprint
        if reason == "source_changed":
            summary = "Workspace authority configuration changed since it was trusted"
        elif reason == "authority_changed":
            summary = "Effective workspace authority changed since it was trusted"
        elif reason == "legacy_record":
            summary = "Workspace trust record must be renewed with a configuration fingerprint"
        else:
            summary = "Workspace is not trusted"
        super().__init__(
            f"{summary}: {self.workspace}. Review {self.config_dir} and run "
            f"`opc trust add {self.workspace}`.",
        )


class WorkspaceTrustStore:
    """User-owned trust store bound to workspace authority fingerprints."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_trust_store_path()

    def _empty_payload(self) -> dict[str, Any]:
        return {"version": _TRUST_STORE_VERSION, "trusted_workspaces": []}

    def load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return self._empty_payload()

        if not isinstance(raw, dict):
            return self._empty_payload()
        if raw.get("version") != _TRUST_STORE_VERSION:
            return self._empty_payload()
        entries = raw.get("trusted_workspaces", [])
        if not isinstance(entries, list):
            return self._empty_payload()
        normalized_entries: dict[str, dict[str, str]] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            workspace = entry.get("workspace")
            source_fingerprint = entry.get("source_fingerprint")
            authority_fingerprint = entry.get("authority_fingerprint")
            if not all(
                isinstance(value, str) and value.strip()
                for value in (workspace, source_fingerprint, authority_fingerprint)
            ):
                continue
            path = Path(workspace).expanduser()
            if not path.is_absolute():
                continue
            canonical = str(canonical_workspace(path))
            normalized_entries[canonical] = {
                "workspace": canonical,
                "source_fingerprint": source_fingerprint,
                "authority_fingerprint": authority_fingerprint,
            }
        normalized = [normalized_entries[key] for key in sorted(normalized_entries)]
        return {"version": _TRUST_STORE_VERSION, "trusted_workspaces": normalized}

    def _save(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass

        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                tmp_path = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                tmp_path.chmod(0o600)
            except OSError:
                pass
            os.replace(tmp_path, self.path)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        except Exception:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

    def _record(self, workspace: str | Path) -> dict[str, str] | None:
        key = str(canonical_workspace(workspace))
        return next(
            (
                entry
                for entry in self.load()["trusted_workspaces"]
                if entry["workspace"] == key
            ),
            None,
        )

    def is_trusted(self, workspace: str | Path) -> bool:
        return self._record(workspace) is not None

    def trust(
        self,
        workspace: str | Path,
        config_dir: str | Path,
        config: Any,
    ) -> Path:
        canonical = canonical_workspace(workspace)
        payload = self.load()
        entries = {
            entry["workspace"]: entry
            for entry in payload["trusted_workspaces"]
            if entry["workspace"] != str(canonical)
        }
        entries[str(canonical)] = {
            "workspace": str(canonical),
            "source_fingerprint": authority_source_fingerprint(config_dir),
            "authority_fingerprint": effective_authority_fingerprint(config),
        }
        self._save(
            {
                "version": _TRUST_STORE_VERSION,
                "trusted_workspaces": [entries[key] for key in sorted(entries)],
            }
        )
        return canonical

    def untrust(self, workspace: str | Path) -> bool:
        canonical = canonical_workspace(workspace)
        payload = self.load()
        entries = {
            entry["workspace"]: entry
            for entry in payload["trusted_workspaces"]
            if entry["workspace"] != str(canonical)
        }
        removed = len(entries) != len(payload["trusted_workspaces"])
        if removed:
            self._save(
                {
                    "version": _TRUST_STORE_VERSION,
                    "trusted_workspaces": [entries[key] for key in sorted(entries)],
                }
            )
        return removed

    def list_trusted(self) -> list[Path]:
        return [Path(entry["workspace"]) for entry in self.load()["trusted_workspaces"]]

    def require(
        self,
        workspace: str | Path,
        config_dir: str | Path,
        config: Any | None = None,
    ) -> None:
        canonical = canonical_workspace(workspace)
        resolved_config_dir = Path(config_dir).resolve(strict=False)
        current_source = authority_source_fingerprint(config_dir)
        record = self._record(canonical)
        if record is None:
            reason = "legacy_record" if self._has_legacy_record(canonical) else "untrusted"
            raise WorkspaceTrustRequired(
                canonical,
                resolved_config_dir,
                reason=reason,
                current_fingerprint=current_source,
            )
        if record["source_fingerprint"] != current_source:
            raise WorkspaceTrustRequired(
                canonical,
                resolved_config_dir,
                reason="source_changed",
                current_fingerprint=current_source,
            )
        if config is not None:
            current_authority = effective_authority_fingerprint(config)
            if record["authority_fingerprint"] != current_authority:
                raise WorkspaceTrustRequired(
                    canonical,
                    resolved_config_dir,
                    reason="authority_changed",
                    current_fingerprint=current_source,
                )

    def _has_legacy_record(self, workspace: Path) -> bool:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return False
        entries = raw.get("trusted_workspaces", []) if isinstance(raw, dict) else []
        if not isinstance(entries, list):
            return False
        key = str(canonical_workspace(workspace))
        return any(
            isinstance(entry, str)
            and Path(entry).expanduser().is_absolute()
            and str(canonical_workspace(entry)) == key
            for entry in entries
        )


def save_explicit_workspace_authority_change(
    config: Any,
    config_dir: str | Path,
) -> Path | None:
    """Persist an explicit UI/CLI authority change without invalidating trust.

    A project cannot call this before it is trusted: the current source
    fingerprint is verified first.  The user-facing permission controls call
    it only after an explicit action.  If either serialization or the
    user-owned trust-store update fails, authority source files are restored
    so the next startup never sees a half-applied change.
    """

    root = Path(config_dir).expanduser().resolve(strict=False)
    workspace = project_workspace_for_config(
        root,
        active_project_root=Path.cwd(),
    )
    if workspace is None:
        config.save(root)
        return None

    store = WorkspaceTrustStore()
    store.require(workspace, root)
    snapshots: dict[Path, bytes | None] = {}
    for relative_name in _AUTHORITY_SOURCE_FILES:
        path = root / relative_name
        try:
            snapshots[path] = path.read_bytes()
        except FileNotFoundError:
            snapshots[path] = None

    try:
        config.save(root)
        normalized = config.__class__.load(root, trusted_source=True)
        store.trust(workspace, root, normalized)
        bind = getattr(config, "bind_workspace_trust", None)
        if callable(bind):
            bind(workspace, root)
        return workspace
    except BaseException:
        for path, payload in snapshots.items():
            if payload is None:
                path.unlink(missing_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    "wb",
                    dir=path.parent,
                    prefix=f".{path.name}.",
                    suffix=".restore",
                    delete=False,
                ) as handle:
                    tmp_path = Path(handle.name)
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_path, path)
            finally:
                if tmp_path is not None:
                    tmp_path.unlink(missing_ok=True)
        raise

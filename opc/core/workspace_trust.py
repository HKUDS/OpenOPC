"""User-owned trust decisions for project-local OpenOPC configuration.

Project configuration is intentionally powerful: it can select executables,
network endpoints, and credential sources.  Trust records therefore live
outside ``OPC_HOME`` so a repository cannot grant trust to itself by
committing files below ``.opc``.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


_TRUST_STORE_VERSION = 1


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

    def __init__(self, workspace: Path, config_dir: Path) -> None:
        self.workspace = workspace
        self.config_dir = config_dir
        super().__init__(
            f"Workspace is not trusted: {self.workspace}. "
            f"Review {self.config_dir} and run `opc trust add {self.workspace}`.",
        )


class WorkspaceTrustStore:
    """Small JSON trust store keyed by canonical workspace path."""

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
        entries = raw.get("trusted_workspaces", [])
        if not isinstance(entries, list):
            return self._empty_payload()
        normalized_entries: set[str] = set()
        for entry in entries:
            if not isinstance(entry, str) or not entry.strip():
                continue
            path = Path(entry).expanduser()
            if path.is_absolute():
                normalized_entries.add(str(canonical_workspace(path)))
        normalized = sorted(normalized_entries)
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

    def is_trusted(self, workspace: str | Path) -> bool:
        key = str(canonical_workspace(workspace))
        return key in self.load()["trusted_workspaces"]

    def trust(self, workspace: str | Path) -> Path:
        canonical = canonical_workspace(workspace)
        payload = self.load()
        entries = set(payload["trusted_workspaces"])
        entries.add(str(canonical))
        self._save(
            {
                "version": _TRUST_STORE_VERSION,
                "trusted_workspaces": sorted(entries),
            }
        )
        return canonical

    def untrust(self, workspace: str | Path) -> bool:
        canonical = canonical_workspace(workspace)
        payload = self.load()
        entries = set(payload["trusted_workspaces"])
        removed = str(canonical) in entries
        entries.discard(str(canonical))
        if removed:
            self._save(
                {
                    "version": _TRUST_STORE_VERSION,
                    "trusted_workspaces": sorted(entries),
                }
            )
        return removed

    def list_trusted(self) -> list[Path]:
        return [Path(entry) for entry in self.load()["trusted_workspaces"]]

    def require(self, workspace: str | Path, config_dir: str | Path) -> None:
        canonical = canonical_workspace(workspace)
        if not self.is_trusted(canonical):
            raise WorkspaceTrustRequired(canonical, Path(config_dir).resolve(strict=False))

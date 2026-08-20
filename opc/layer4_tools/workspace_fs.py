"""Race-safe filesystem capabilities for task-scoped native tools.

For a task carrying a durable workspace root, path authorization and the
filesystem effect must be one operation.  Canonicalizing a pathname and then
using :class:`pathlib.Path` leaves a check/use window in which an intermediate
directory can be replaced by a symlink.  This module instead pins the
workspace directory and walks every component relative to already-open
directory descriptors with ``O_NOFOLLOW``.
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator


class WorkspaceBoundaryError(PermissionError):
    """A task-scoped filesystem operation could not stay within its workspace."""


_INTERNAL_TEMP_PREFIX = ".opc-write-"
_RUNTIME_INTERNAL_COMPONENT = ".opc-comms"


def _reserved_internal_component(name: str) -> bool:
    normalized = str(name or "")
    return normalized.startswith(_INTERNAL_TEMP_PREFIX) or normalized == _RUNTIME_INTERNAL_COMPONENT


def is_model_reserved_workspace_path(path: str) -> bool:
    """Whether a model-authored path names runtime-owned workspace state."""

    try:
        parts = Path(os.path.normpath(os.path.expanduser(str(path or "")))).parts
    except (OSError, RuntimeError, TypeError, ValueError):
        return True
    return any(_reserved_internal_component(part) for part in parts)


@dataclass(frozen=True)
class WorkspacePath:
    """A lexical path relative to one pinned workspace capability."""

    parts: tuple[str, ...]
    display_path: Path


@dataclass(frozen=True)
class WorkspaceEntry:
    """A symlink-free entry observed through a pinned directory descriptor."""

    parts: tuple[str, ...]
    relative_path: str
    is_dir: bool
    size: int


def _secure_primitives_available() -> bool:
    required_dir_fd = (os.open, os.mkdir, os.stat, os.unlink, os.rename)
    return bool(
        os.name == "posix"
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
        and all(function in os.supports_dir_fd for function in required_dir_fd)
        and os.stat in os.supports_follow_symlinks
        and os.listdir in os.supports_fd
    )


def workspace_roots_for_task(task: Any) -> tuple[str, str]:
    """Return the exact roots used by task-scoped filesystem effects.

    Callers that authorize an effect must compare these in-memory capability
    hints with their durable roots.  Keeping this resolver public and shared
    prevents the authorization path from drifting from ``SecureWorkspace``.
    """

    metadata = dict(getattr(task, "metadata", {}) or {})
    execution_context = dict(metadata.get("_execution_context", {}) or {})

    # An isolated native subagent inherits the parent's top-level metadata but
    # receives its worktree in the runtime-owned execution context.  Therefore
    # a populated execution-context root is the current capability; top-level
    # metadata remains the durable fallback for ordinary company role tasks.
    context_workspace = str(execution_context.get("workspace_root", "") or "").strip()
    if context_workspace:
        workspace = context_workspace
        output = (
            str(execution_context.get("output_root", "") or "").strip()
            or context_workspace
        )
        return workspace, output

    workspace = (
        str(metadata.get("workspace_root", "") or "").strip()
        or str(metadata.get("comms_workspace_root", "") or "").strip()
    )
    output = (
        str(metadata.get("output_root", "") or "").strip()
        or str(metadata.get("target_output_dir", "") or "").strip()
        or workspace
    )
    return workspace, output


def _absolute_normalized(path: str) -> Path:
    try:
        expanded = os.path.expanduser(str(path or ""))
        return Path(os.path.abspath(os.path.normpath(expanded)))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise WorkspaceBoundaryError(
            "Path cannot be represented within the task workspace boundary."
        ) from exc


def _relative_parts(root: Path, candidate: Path) -> tuple[str, ...]:
    try:
        common = Path(os.path.commonpath((str(root), str(candidate))))
    except (OSError, ValueError) as exc:
        raise WorkspaceBoundaryError(
            f"Path is outside the task workspace boundary: {candidate}"
        ) from exc
    if common != root:
        raise WorkspaceBoundaryError(
            f"Path is outside the task workspace boundary: {candidate} "
            f"(workspace: {root})"
        )
    relative = os.path.relpath(str(candidate), str(root))
    if relative == ".":
        return ()
    parts = tuple(Path(relative).parts)
    if any(part in {"", ".", ".."} or os.sep in part for part in parts):
        raise WorkspaceBoundaryError(
            f"Path is outside the task workspace boundary: {candidate}"
        )
    return parts


class SecureWorkspace:
    """A pinned workspace directory used for one complete tool effect."""

    def __init__(self, workspace_root: str, output_root: str) -> None:
        self.root = _absolute_normalized(workspace_root)
        self.output = _absolute_normalized(output_root or workspace_root)
        _relative_parts(self.root, self.output)
        self._root_fd: int | None = None

    @classmethod
    def for_task(cls, task: Any | None) -> SecureWorkspace | None:
        if task is None:
            return None
        workspace, output = workspace_roots_for_task(task)
        if not workspace:
            return None
        return cls(workspace, output)

    def __enter__(self) -> SecureWorkspace:
        if not _secure_primitives_available():
            raise WorkspaceBoundaryError(
                "Task workspace boundary cannot be enforced on this platform; "
                "filesystem tool execution is disabled."
            )
        self._root_fd = self._open_absolute_directory(self.root)
        return self

    def __exit__(self, *_: object) -> None:
        if self._root_fd is not None:
            os.close(self._root_fd)
            self._root_fd = None

    @property
    def root_fd(self) -> int:
        if self._root_fd is None:
            raise RuntimeError("SecureWorkspace must be entered before use")
        return self._root_fd

    @staticmethod
    def _directory_flags() -> int:
        return (
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )

    def _open_absolute_directory(self, path: Path) -> int:
        current = os.open(os.path.sep, self._directory_flags())
        try:
            for component in path.parts:
                if component in {os.path.sep, ""}:
                    continue
                next_fd = self._open_directory_component(current, component)
                os.close(current)
                current = next_fd
            return current
        except BaseException:
            os.close(current)
            raise

    def _open_directory_component(self, parent_fd: int, component: str) -> int:
        try:
            return os.open(
                component,
                self._directory_flags(),
                dir_fd=parent_fd,
            )
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise WorkspaceBoundaryError(
                    "Symlink or directory swap rejected by the task workspace boundary: "
                    f"{component}"
                ) from exc
            raise

    def resolve(
        self,
        path: str,
        *,
        use_output_root: bool = True,
        allow_runtime_internal_read: bool = False,
    ) -> WorkspacePath:
        raw = str(path or "")
        candidate = Path(os.path.expanduser(raw))
        if candidate.is_absolute():
            absolute = _absolute_normalized(raw)
        else:
            base = self.output if use_output_root else self.root
            absolute = _absolute_normalized(str(base / candidate))
        parts = _relative_parts(self.root, absolute)
        if any(part.startswith(_INTERNAL_TEMP_PREFIX) for part in parts):
            raise WorkspaceBoundaryError(
                "Path belongs to the runtime-internal workspace namespace."
            )
        if (
            any(part == _RUNTIME_INTERNAL_COMPONENT for part in parts)
            and not allow_runtime_internal_read
        ):
            raise WorkspaceBoundaryError(
                "Path belongs to the runtime-internal workspace namespace."
            )
        return WorkspacePath(parts=parts, display_path=self.root.joinpath(*parts))

    @contextmanager
    def _open_parent(
        self,
        target: WorkspacePath,
        *,
        create_dirs: bool = False,
    ) -> Iterator[tuple[int, str]]:
        if not target.parts:
            raise IsADirectoryError(str(target.display_path))
        current = os.dup(self.root_fd)
        try:
            for component in target.parts[:-1]:
                try:
                    next_fd = self._open_directory_component(current, component)
                except FileNotFoundError:
                    if not create_dirs:
                        raise
                    try:
                        os.mkdir(component, mode=0o777, dir_fd=current)
                    except FileExistsError:
                        # A concurrent creator won.  The no-follow open below
                        # decides whether it created a real child directory.
                        pass
                    next_fd = self._open_directory_component(current, component)
                os.close(current)
                current = next_fd
            yield current, target.parts[-1]
        finally:
            os.close(current)

    def open_directory(self, target: WorkspacePath) -> int:
        current = os.dup(self.root_fd)
        try:
            for component in target.parts:
                next_fd = self._open_directory_component(current, component)
                os.close(current)
                current = next_fd
            return current
        except BaseException:
            os.close(current)
            raise

    def ensure_runtime_directory(self, target: WorkspacePath) -> None:
        """Create a runtime-internal directory tree through pinned dirfds."""

        if _RUNTIME_INTERNAL_COMPONENT not in target.parts:
            raise WorkspaceBoundaryError(
                "Runtime directory must stay inside the internal workspace namespace."
            )
        current = os.dup(self.root_fd)
        try:
            for component in target.parts:
                try:
                    next_fd = self._open_directory_component(current, component)
                except FileNotFoundError:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=current)
                    except FileExistsError:
                        pass
                    next_fd = self._open_directory_component(current, component)
                os.close(current)
                current = next_fd
        finally:
            os.close(current)

    @staticmethod
    def _regular_file(fd: int, display_path: Path) -> None:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise WorkspaceBoundaryError(
                "Non-regular or multiply-linked file rejected by the task workspace boundary: "
                f"{display_path}"
            )

    @staticmethod
    def _read_fd(fd: int) -> str:
        os.lseek(fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")

    @staticmethod
    def _write_fd(fd: int, content: str) -> int:
        payload = content.encode("utf-8")
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        written = 0
        while written < len(payload):
            count = os.write(fd, payload[written:])
            if count <= 0:
                raise OSError("Short write while updating workspace file")
            written += count
        return written

    def _atomic_replace_text(
        self,
        parent_fd: int,
        name: str,
        content: str,
        *,
        mode: int,
        preserve_mode: bool,
        keep_receipt_fd: bool = False,
    ) -> tuple[int, tuple[int, int, int], int | None]:
        temp_name = ""
        temp_fd: int | None = None
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            for _ in range(32):
                temp_name = f"{_INTERNAL_TEMP_PREFIX}{secrets.token_hex(12)}.tmp"
                try:
                    temp_fd = os.open(
                        temp_name,
                        flags,
                        mode=mode,
                        dir_fd=parent_fd,
                    )
                    break
                except FileExistsError:
                    continue
            if temp_fd is None:
                raise FileExistsError(
                    "Unable to allocate a unique temporary workspace file"
                )
            if preserve_mode:
                os.fchmod(temp_fd, mode)
            bytes_written = self._write_fd(temp_fd, content)
            # A successful receipt means both the bytes and the directory
            # entry survived the atomic publication boundary.  If either
            # sync fails, propagate the failure rather than reporting a
            # durable runtime artifact that may disappear after a crash.
            os.fsync(temp_fd)
            written_info = os.fstat(temp_fd)
            written_identity = (
                int(written_info.st_dev),
                int(written_info.st_ino),
                int(written_info.st_size),
            )
            os.rename(
                temp_name,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            temp_name = ""
            os.fsync(parent_fd)
            receipt_fd: int | None = None
            if keep_receipt_fd:
                receipt_fd = temp_fd
                temp_fd = None
            else:
                os.close(temp_fd)
                temp_fd = None
            return bytes_written, written_identity, receipt_fd
        finally:
            if temp_fd is not None:
                os.close(temp_fd)
            if temp_name:
                try:
                    os.unlink(temp_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass

    def _open_file(self, target: WorkspacePath, flags: int) -> int:
        with self._open_parent(target) as (parent_fd, name):
            try:
                fd = os.open(
                    name,
                    flags
                    | os.O_NOFOLLOW
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NONBLOCK", 0),
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise WorkspaceBoundaryError(
                        "Symlink or path swap rejected by the task workspace boundary: "
                        f"{target.display_path}"
                    ) from exc
                raise
        try:
            self._regular_file(fd, target.display_path)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def read_text(self, target: WorkspacePath) -> str:
        fd = self._open_file(target, os.O_RDONLY)
        try:
            return self._read_fd(fd)
        finally:
            os.close(fd)

    def write_text(
        self,
        target: WorkspacePath,
        content: str,
        *,
        create_dirs: bool,
    ) -> tuple[str, bool, int]:
        with self._open_parent(target, create_dirs=create_dirs) as (parent_fd, name):
            read_flags = (
                os.O_RDONLY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            created = False
            try:
                fd = os.open(name, read_flags, dir_fd=parent_fd)
            except FileNotFoundError:
                fd = None
                created = True
                before = ""
                mode = 0o666
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise WorkspaceBoundaryError(
                        "Symlink or path swap rejected by the task workspace boundary: "
                        f"{target.display_path}"
                    ) from exc
                raise
            if fd is not None:
                try:
                    self._regular_file(fd, target.display_path)
                    before = self._read_fd(fd)
                    mode = stat.S_IMODE(os.fstat(fd).st_mode) & 0o777
                finally:
                    os.close(fd)
            bytes_written, _, _ = self._atomic_replace_text(
                parent_fd,
                name,
                content,
                mode=mode,
                preserve_mode=not created,
            )
            return before, created, bytes_written

    def write_runtime_text(
        self,
        target: WorkspacePath,
        content: str,
        *,
        create_dirs: bool,
    ) -> int:
        """Write one runtime-owned file without exposing a model capability.

        The internal namespace is reached through pinned, no-follow directory
        descriptors.  After the atomic replacement, reopen the lexical parent
        from the workspace root and require it to name the same inode.  Thus a
        concurrent rename/symlink substitution cannot turn the returned path
        into an alias for an attacker-controlled location.
        """

        if _RUNTIME_INTERNAL_COMPONENT not in target.parts:
            raise WorkspaceBoundaryError(
                "Runtime output must stay inside the internal workspace namespace."
            )
        with self._open_parent(target, create_dirs=create_dirs) as (parent_fd, name):
            (
                bytes_written,
                expected_identity,
                receipt_fd,
            ) = self._atomic_replace_text(
                parent_fd,
                name,
                content,
                mode=0o600,
                preserve_mode=False,
                keep_receipt_fd=True,
            )
            assert receipt_fd is not None
            try:
                try:
                    with self._open_parent(target) as (
                        current_parent_fd,
                        current_name,
                    ):
                        original_parent = os.fstat(parent_fd)
                        current_parent = os.fstat(current_parent_fd)
                        still_named = bool(
                            current_name == name
                            and original_parent.st_dev == current_parent.st_dev
                            and original_parent.st_ino == current_parent.st_ino
                        )
                except (
                    FileNotFoundError,
                    NotADirectoryError,
                    WorkspaceBoundaryError,
                ):
                    still_named = False
                if not still_named:
                    try:
                        os.unlink(name, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass
                    raise WorkspaceBoundaryError(
                        "Runtime output parent changed during the filesystem effect."
                    )
                current_fd = self._open_file(target, os.O_RDONLY)
                try:
                    current_info = os.fstat(current_fd)
                    current_identity = (
                        int(current_info.st_dev),
                        int(current_info.st_ino),
                        int(current_info.st_size),
                    )
                finally:
                    os.close(current_fd)
                receipt_info = os.fstat(receipt_fd)
                receipt_identity = (
                    int(receipt_info.st_dev),
                    int(receipt_info.st_ino),
                    int(receipt_info.st_size),
                )
                if (
                    current_identity != expected_identity
                    or receipt_identity != expected_identity
                ):
                    raise WorkspaceBoundaryError(
                        "Runtime output target changed during the filesystem effect."
                    )
            finally:
                os.close(receipt_fd)
            return bytes_written

    @contextmanager
    def open_runtime_append(
        self,
        target: WorkspacePath,
        *,
        create: bool,
    ) -> Iterator[int]:
        """Open one runtime file for append and reject parent-name swaps."""

        if _RUNTIME_INTERNAL_COMPONENT not in target.parts:
            raise WorkspaceBoundaryError(
                "Runtime append must stay inside the internal workspace namespace."
            )
        with self._open_parent(target) as (parent_fd, name):
            flags = (
                os.O_WRONLY
                | os.O_APPEND
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0)
            )
            if create:
                flags |= os.O_CREAT
            fd = os.open(name, flags, mode=0o600, dir_fd=parent_fd)
            try:
                self._regular_file(fd, target.display_path)
                yield fd
                appended_info = os.fstat(fd)
                appended_identity = (
                    int(appended_info.st_dev),
                    int(appended_info.st_ino),
                    int(appended_info.st_size),
                )
                with self._open_parent(target) as (current_parent_fd, current_name):
                    original_parent = os.fstat(parent_fd)
                    current_parent = os.fstat(current_parent_fd)
                    if not (
                        current_name == name
                        and original_parent.st_dev == current_parent.st_dev
                        and original_parent.st_ino == current_parent.st_ino
                    ):
                        raise WorkspaceBoundaryError(
                            "Runtime append parent changed during the filesystem effect."
                        )
                current_fd = self._open_file(target, os.O_RDONLY)
                try:
                    current_info = os.fstat(current_fd)
                    current_identity = (
                        int(current_info.st_dev),
                        int(current_info.st_ino),
                        int(current_info.st_size),
                    )
                finally:
                    os.close(current_fd)
                if current_identity != appended_identity:
                    raise WorkspaceBoundaryError(
                        "Runtime append target changed during the filesystem effect."
                    )
            finally:
                os.close(fd)

    def mutate_text(
        self,
        target: WorkspacePath,
        transform: Callable[[str], str],
    ) -> tuple[str, str, int]:
        with self._open_parent(target) as (parent_fd, name):
            flags = (
                os.O_RDONLY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            try:
                fd = os.open(name, flags, dir_fd=parent_fd)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise WorkspaceBoundaryError(
                        "Symlink or path swap rejected by the task workspace boundary: "
                        f"{target.display_path}"
                    ) from exc
                raise
            try:
                self._regular_file(fd, target.display_path)
                before = self._read_fd(fd)
                mode = stat.S_IMODE(os.fstat(fd).st_mode) & 0o777
            finally:
                os.close(fd)
            after = transform(before)
            bytes_written, _, _ = self._atomic_replace_text(
                parent_fd,
                name,
                after,
                mode=mode,
                preserve_mode=True,
            )
            return before, after, bytes_written

    def unlink(self, target: WorkspacePath) -> None:
        with self._open_parent(target) as (parent_fd, name):
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise WorkspaceBoundaryError(
                    "Non-regular file rejected by the task workspace boundary: "
                    f"{target.display_path}"
                )
            os.unlink(name, dir_fd=parent_fd)

    def rename(self, source: WorkspacePath, target: WorkspacePath) -> None:
        with self._open_parent(source) as (source_fd, source_name):
            held_source_fd = os.open(
                source_name,
                os.O_RDONLY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=source_fd,
            )
            try:
                self._regular_file(held_source_fd, source.display_path)
                held_info = os.fstat(held_source_fd)
                held_identity = (
                    int(held_info.st_dev),
                    int(held_info.st_ino),
                    int(held_info.st_size),
                )
                with self._open_parent(target, create_dirs=True) as (
                    target_fd,
                    target_name,
                ):
                    try:
                        target_info = os.stat(
                            target_name,
                            dir_fd=target_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        target_info = None
                    if target_info is not None and (
                        stat.S_ISLNK(target_info.st_mode)
                        or not stat.S_ISREG(target_info.st_mode)
                    ):
                        raise WorkspaceBoundaryError(
                            "Non-regular file rejected by the task workspace boundary: "
                            f"{target.display_path}"
                        )
                    os.rename(
                        source_name,
                        target_name,
                        src_dir_fd=source_fd,
                        dst_dir_fd=target_fd,
                    )
                    os.fsync(source_fd)
                    source_parent_info = os.fstat(source_fd)
                    target_parent_info = os.fstat(target_fd)
                    if (
                        source_parent_info.st_dev,
                        source_parent_info.st_ino,
                    ) != (
                        target_parent_info.st_dev,
                        target_parent_info.st_ino,
                    ):
                        os.fsync(target_fd)

                    # ``renameat`` acts on pinned parents, but callers need a
                    # receipt for the lexical target they will subsequently
                    # publish.  Reopen both parents from the workspace root and
                    # require the final target to be the still-open source inode.
                    try:
                        with self._open_parent(source) as (
                            current_source_parent_fd,
                            current_source_name,
                        ), self._open_parent(target) as (
                            current_target_parent_fd,
                            current_target_name,
                        ):
                            original_source_parent = os.fstat(source_fd)
                            current_source_parent = os.fstat(
                                current_source_parent_fd
                            )
                            original_target_parent = os.fstat(target_fd)
                            current_target_parent = os.fstat(
                                current_target_parent_fd
                            )
                            parents_still_named = bool(
                                current_source_name == source_name
                                and current_target_name == target_name
                                and original_source_parent.st_dev
                                == current_source_parent.st_dev
                                and original_source_parent.st_ino
                                == current_source_parent.st_ino
                                and original_target_parent.st_dev
                                == current_target_parent.st_dev
                                and original_target_parent.st_ino
                                == current_target_parent.st_ino
                            )
                    except (
                        FileNotFoundError,
                        NotADirectoryError,
                        WorkspaceBoundaryError,
                    ):
                        parents_still_named = False
                    if not parents_still_named:
                        raise WorkspaceBoundaryError(
                            "Runtime rename parent changed during the filesystem effect."
                        )

                    current_fd = self._open_file(target, os.O_RDONLY)
                    try:
                        current_info = os.fstat(current_fd)
                        current_identity = (
                            int(current_info.st_dev),
                            int(current_info.st_ino),
                            int(current_info.st_size),
                        )
                    finally:
                        os.close(current_fd)
                    if current_identity != held_identity:
                        raise WorkspaceBoundaryError(
                            "Runtime rename target changed during the filesystem effect."
                        )
            finally:
                os.close(held_source_fd)

    def iter_entries(
        self,
        target: WorkspacePath,
        *,
        recursive: bool,
        max_depth: int | None = None,
    ) -> Iterator[WorkspaceEntry]:
        directory_fd = self.open_directory(target)

        def _walk(
            current_fd: int,
            prefix: tuple[str, ...],
            depth: int,
        ) -> Iterator[WorkspaceEntry]:
            try:
                names = sorted(os.listdir(current_fd), key=str.lower)
            except (FileNotFoundError, NotADirectoryError, PermissionError):
                return
            for name in names:
                if _reserved_internal_component(name):
                    continue
                try:
                    info = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
                except (FileNotFoundError, NotADirectoryError, PermissionError):
                    continue
                # Never expose or traverse a symlink from a task-scoped file
                # tool.  A later open also uses O_NOFOLLOW, so a swap after
                # this observation remains fail closed.
                if stat.S_ISLNK(info.st_mode):
                    continue
                is_dir = stat.S_ISDIR(info.st_mode)
                is_file = stat.S_ISREG(info.st_mode)
                if not is_dir and not is_file:
                    continue
                parts = (*prefix, name)
                yield WorkspaceEntry(
                    parts=parts,
                    relative_path=str(Path(*parts)),
                    is_dir=is_dir,
                    size=int(info.st_size) if is_file else 0,
                )
                if not recursive or not is_dir:
                    continue
                if max_depth is not None and depth >= max_depth:
                    continue
                try:
                    child_fd = self._open_directory_component(current_fd, name)
                except (FileNotFoundError, NotADirectoryError, PermissionError, WorkspaceBoundaryError):
                    continue
                try:
                    yield from _walk(child_fd, parts, depth + 1)
                finally:
                    os.close(child_fd)

        try:
            yield from _walk(directory_fd, (), 0)
        finally:
            os.close(directory_fd)

"""Windows handle-relative backend for :mod:`opc.layer4_tools.workspace_fs`.

Python does not expose ``openat``-style filesystem operations on Windows.  A
pathname-only fallback would reintroduce the exact check/use race that
``SecureWorkspace`` exists to prevent, especially through junctions and other
reparse points.  This backend pins the workspace and walks/creates every child
relative to already-open directory handles through ``NtCreateFile``.

The module is imported lazily on Windows.  Keeping the platform binding here
lets every caller retain the same small ``SecureWorkspace`` capability API.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import os
import secrets
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from opc.layer4_tools.workspace_fs import (
    WorkspaceBoundaryError,
    WorkspaceEntry,
    WorkspacePath,
    _INTERNAL_TEMP_PREFIX,
    RUNTIME_INTERNAL_WORKSPACE_COMPONENT,
    _absolute_normalized,
    _relative_parts,
    _reserved_internal_component,
    workspace_roots_for_task,
)


if os.name == "nt":  # pragma: win32 cover
    import msvcrt
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")
else:  # Keep imports/introspection safe on POSIX test hosts.
    msvcrt = None  # type: ignore[assignment]
    wintypes = None  # type: ignore[assignment]
    _kernel32 = None
    _ntdll = None


# Access masks and native create options from winnt.h / ntifs.h.
_DELETE = 0x00010000
_SYNCHRONIZE = 0x00100000
_FILE_READ_DATA = 0x0001
_FILE_LIST_DIRECTORY = 0x0001
_FILE_WRITE_DATA = 0x0002
_FILE_APPEND_DATA = 0x0004
_FILE_READ_ATTRIBUTES = 0x0080
_FILE_TRAVERSE = 0x0020
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_FILE_SUPERSEDE = 0
_FILE_OPEN = 1
_FILE_CREATE = 2
_FILE_OPEN_IF = 3
_FILE_OVERWRITE = 4
_FILE_OVERWRITE_IF = 5
_FILE_DIRECTORY_FILE = 0x00000001
_FILE_NON_DIRECTORY_FILE = 0x00000040
_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_FILE_OPEN_REPARSE_POINT = 0x00200000
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_OPEN_EXISTING = 3
_OBJ_CASE_INSENSITIVE = 0x00000040
_FILE_RENAME_INFO_CLASS = 3
_FILE_DISPOSITION_INFO_CLASS = 4
_FILE_DIRECTORY_INFORMATION_CLASS = 1
_STATUS_NO_MORE_FILES = 0x80000006
_STATUS_BUFFER_OVERFLOW = 0x80000005
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3
_ERROR_ACCESS_DENIED = 5
_ERROR_FILE_EXISTS = 80
_ERROR_ALREADY_EXISTS = 183
_ERROR_DIRECTORY = 267

_RESERVED_DOS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


if os.name == "nt":  # pragma: win32 cover
    _NTSTATUS = ctypes.c_long
    _ULONG_PTR = ctypes.c_size_t

    class _UNICODE_STRING(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", wintypes.LPWSTR),
        ]

    class _OBJECT_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.ULONG),
            ("RootDirectory", wintypes.HANDLE),
            ("ObjectName", ctypes.POINTER(_UNICODE_STRING)),
            ("Attributes", wintypes.ULONG),
            ("SecurityDescriptor", wintypes.LPVOID),
            ("SecurityQualityOfService", wintypes.LPVOID),
        ]

    class _IO_STATUS_BLOCK(ctypes.Structure):
        _fields_ = [("Status", _NTSTATUS), ("Information", _ULONG_PTR)]

    class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    class _FILE_RENAME_INFO(ctypes.Structure):
        _fields_ = [
            ("ReplaceIfExists", ctypes.c_ubyte),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * 1),
        ]

    class _FILE_DISPOSITION_INFO(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOL)]

    class _FILE_DIRECTORY_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("NextEntryOffset", wintypes.ULONG),
            ("FileIndex", wintypes.ULONG),
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("EndOfFile", ctypes.c_longlong),
            ("AllocationSize", ctypes.c_longlong),
            ("FileAttributes", wintypes.ULONG),
            ("FileNameLength", wintypes.ULONG),
            ("FileName", wintypes.WCHAR * 1),
        ]

    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.DuplicateHandle.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    _kernel32.DuplicateHandle.restype = wintypes.BOOL
    _kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION),
    ]
    _kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    _kernel32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    _kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    _kernel32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _kernel32.SetFileInformationByHandle.restype = wintypes.BOOL

    _ntdll.NtCreateFile.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.POINTER(_OBJECT_ATTRIBUTES),
        ctypes.POINTER(_IO_STATUS_BLOCK),
        ctypes.c_void_p,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        ctypes.c_void_p,
        wintypes.ULONG,
    ]
    _ntdll.NtCreateFile.restype = _NTSTATUS
    _ntdll.NtQueryDirectoryFile.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(_IO_STATUS_BLOCK),
        wintypes.LPVOID,
        wintypes.ULONG,
        ctypes.c_int,
        wintypes.BOOLEAN,
        ctypes.c_void_p,
        wintypes.BOOLEAN,
    ]
    _ntdll.NtQueryDirectoryFile.restype = _NTSTATUS
    _ntdll.RtlNtStatusToDosError.argtypes = [_NTSTATUS]
    _ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG


def _require_windows() -> None:
    if os.name != "nt" or _kernel32 is None or _ntdll is None:
        raise WorkspaceBoundaryError(
            "The Windows secure workspace backend is unavailable on this platform."
        )


def windows_backend_available() -> bool:
    """Return whether the native Windows handle API was loaded successfully."""

    return bool(os.name == "nt" and _kernel32 is not None and _ntdll is not None)


def _validate_component(component: str) -> None:
    value = str(component or "")
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or ":" in value
        or value.endswith((" ", "."))
        or value.split(".", 1)[0].upper() in _RESERVED_DOS_NAMES
    ):
        raise WorkspaceBoundaryError(
            f"Unsupported Windows workspace path component: {component!r}"
        )


def _extended_path_to_dos(path: str) -> str:
    value = str(path or "")
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


class WindowsSecureWorkspace:
    """Pinned, reparse-point-free Windows workspace capability."""

    def __init__(self, workspace_root: str, output_root: str) -> None:
        _require_windows()
        self.root = _absolute_normalized(workspace_root)
        self.output = _absolute_normalized(output_root or workspace_root)
        _relative_parts(self.root, self.output)
        self._root_handle: int | None = None

    @classmethod
    def for_task(cls, task: object | None) -> WindowsSecureWorkspace | None:
        if task is None:
            return None
        workspace, output = workspace_roots_for_task(task)
        if not workspace:
            return None
        return cls(workspace, output)

    def __enter__(self) -> WindowsSecureWorkspace:
        self._root_handle = self._open_absolute_root(self.root)
        return self

    def __exit__(self, *_: object) -> None:
        if self._root_handle is not None:
            self._close_handle(self._root_handle)
            self._root_handle = None

    @property
    def root_handle(self) -> int:
        if self._root_handle is None:
            raise RuntimeError("SecureWorkspace must be entered before use")
        return self._root_handle

    @staticmethod
    def _close_handle(handle: int) -> None:
        if handle and handle != _INVALID_HANDLE_VALUE:
            _kernel32.CloseHandle(wintypes.HANDLE(handle))

    @staticmethod
    def _duplicate_handle(handle: int) -> int:
        process = _kernel32.GetCurrentProcess()
        duplicate = wintypes.HANDLE()
        if not _kernel32.DuplicateHandle(
            process,
            wintypes.HANDLE(handle),
            process,
            ctypes.byref(duplicate),
            0,
            False,
            0x00000002,  # DUPLICATE_SAME_ACCESS
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(duplicate.value)

    @staticmethod
    def _raise_status(status: int, name: str) -> None:
        code = int(_ntdll.RtlNtStatusToDosError(_NTSTATUS(status)))
        message = ctypes.FormatError(code).strip() or f"Windows error {code}"
        if code in {_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND}:
            raise FileNotFoundError(errno.ENOENT, message, name, code)
        if code in {_ERROR_FILE_EXISTS, _ERROR_ALREADY_EXISTS}:
            raise FileExistsError(errno.EEXIST, message, name, code)
        if code == _ERROR_ACCESS_DENIED:
            raise PermissionError(errno.EACCES, message, name, code)
        if code == _ERROR_DIRECTORY:
            raise NotADirectoryError(errno.ENOTDIR, message, name, code)
        raise OSError(code, message, name, code)

    @staticmethod
    def _file_info(handle: int) -> _BY_HANDLE_FILE_INFORMATION:
        info = _BY_HANDLE_FILE_INFORMATION()
        if not _kernel32.GetFileInformationByHandle(
            wintypes.HANDLE(handle), ctypes.byref(info)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return info

    @classmethod
    def _identity(cls, handle: int) -> tuple[int, int]:
        info = cls._file_info(handle)
        index = (int(info.nFileIndexHigh) << 32) | int(info.nFileIndexLow)
        return int(info.dwVolumeSerialNumber), index

    @classmethod
    def _file_identity(cls, handle: int) -> tuple[int, int, int]:
        info = cls._file_info(handle)
        index = (int(info.nFileIndexHigh) << 32) | int(info.nFileIndexLow)
        size = (int(info.nFileSizeHigh) << 32) | int(info.nFileSizeLow)
        return int(info.dwVolumeSerialNumber), index, size

    @classmethod
    def _validate_handle(
        cls,
        handle: int,
        display_path: Path,
        *,
        directory: bool,
    ) -> None:
        info = cls._file_info(handle)
        attributes = int(info.dwFileAttributes)
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise WorkspaceBoundaryError(
                f"Reparse point rejected by the task workspace boundary: {display_path}"
            )
        is_directory = bool(attributes & _FILE_ATTRIBUTE_DIRECTORY)
        if is_directory != directory:
            kind = "directory" if directory else "regular file"
            raise WorkspaceBoundaryError(
                f"Expected {kind} inside the task workspace boundary: {display_path}"
            )
        if not directory and int(info.nNumberOfLinks) != 1:
            raise WorkspaceBoundaryError(
                "Multiply-linked file rejected by the task workspace boundary: "
                f"{display_path}"
            )

    @staticmethod
    def _final_path(handle: int) -> str:
        size = 512
        while True:
            buffer = ctypes.create_unicode_buffer(size)
            result = int(
                _kernel32.GetFinalPathNameByHandleW(
                    wintypes.HANDLE(handle), buffer, size, 0
                )
            )
            if result == 0:
                raise ctypes.WinError(ctypes.get_last_error())
            if result < size:
                return _extended_path_to_dos(buffer.value)
            size = result + 1

    @classmethod
    def _open_absolute_root(cls, path: Path) -> int:
        requested = str(path)
        if requested.startswith("\\\\"):
            raise WorkspaceBoundaryError(
                "Windows secure workspaces must use a local volume; "
                "UNC workspaces are not supported."
            )
        handle = _kernel32.CreateFileW(
            requested,
            _FILE_LIST_DIRECTORY
            | _FILE_TRAVERSE
            | _FILE_READ_ATTRIBUTES
            | _SYNCHRONIZE,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        raw_handle = int(handle) if handle is not None else 0
        if raw_handle == _INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error(), requested)
        try:
            cls._validate_handle(raw_handle, path, directory=True)
            opened = os.path.normcase(os.path.normpath(cls._final_path(raw_handle)))
            expected = os.path.normcase(os.path.normpath(requested))
            if opened != expected:
                raise WorkspaceBoundaryError(
                    "Workspace root resolves through a reparse point or mount boundary: "
                    f"{path} -> {opened}"
                )
            return raw_handle
        except BaseException:
            cls._close_handle(raw_handle)
            raise

    @classmethod
    def _open_relative_handle(
        cls,
        parent: int,
        name: str,
        *,
        access: int,
        disposition: int,
        directory: bool,
    ) -> int:
        _validate_component(name)
        name_buffer = ctypes.create_unicode_buffer(name)
        encoded_name = name.encode("utf-16-le")
        name_value = _UNICODE_STRING(
            Length=len(encoded_name),
            MaximumLength=len(encoded_name) + ctypes.sizeof(wintypes.WCHAR),
            Buffer=ctypes.cast(name_buffer, wintypes.LPWSTR),
        )
        attributes = _OBJECT_ATTRIBUTES(
            Length=ctypes.sizeof(_OBJECT_ATTRIBUTES),
            RootDirectory=wintypes.HANDLE(parent),
            ObjectName=ctypes.pointer(name_value),
            Attributes=_OBJ_CASE_INSENSITIVE,
            SecurityDescriptor=None,
            SecurityQualityOfService=None,
        )
        io_status = _IO_STATUS_BLOCK()
        result = wintypes.HANDLE()
        options = (
            (_FILE_DIRECTORY_FILE if directory else _FILE_NON_DIRECTORY_FILE)
            | _FILE_OPEN_REPARSE_POINT
            | _FILE_SYNCHRONOUS_IO_NONALERT
        )
        status = int(
            _ntdll.NtCreateFile(
                ctypes.byref(result),
                access | _SYNCHRONIZE,
                ctypes.byref(attributes),
                ctypes.byref(io_status),
                None,
                _FILE_ATTRIBUTE_NORMAL,
                _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
                disposition,
                options,
                None,
                0,
            )
        )
        if status < 0:
            cls._raise_status(status, name)
        handle = int(result.value)
        try:
            cls._validate_handle(handle, Path(name), directory=directory)
            return handle
        except BaseException:
            cls._close_handle(handle)
            raise

    @classmethod
    def _open_directory_component(cls, parent: int, component: str) -> int:
        return cls._open_relative_handle(
            parent,
            component,
            access=_FILE_LIST_DIRECTORY | _FILE_TRAVERSE | _FILE_READ_ATTRIBUTES,
            disposition=_FILE_OPEN,
            directory=True,
        )

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
        for part in parts:
            _validate_component(part)
        if any(part.startswith(_INTERNAL_TEMP_PREFIX) for part in parts):
            raise WorkspaceBoundaryError(
                "Path belongs to the runtime-internal workspace namespace."
            )
        if (
            any(part == RUNTIME_INTERNAL_WORKSPACE_COMPONENT for part in parts)
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
        current = self._duplicate_handle(self.root_handle)
        try:
            for component in target.parts[:-1]:
                try:
                    following = self._open_directory_component(current, component)
                except FileNotFoundError:
                    if not create_dirs:
                        raise
                    try:
                        created = self._open_relative_handle(
                            current,
                            component,
                            access=_FILE_LIST_DIRECTORY
                            | _FILE_TRAVERSE
                            | _FILE_READ_ATTRIBUTES,
                            disposition=_FILE_CREATE,
                            directory=True,
                        )
                    except FileExistsError:
                        created = self._open_directory_component(current, component)
                    following = created
                self._close_handle(current)
                current = following
            yield current, target.parts[-1]
        finally:
            self._close_handle(current)

    def _open_directory_handle(self, target: WorkspacePath) -> int:
        current = self._duplicate_handle(self.root_handle)
        try:
            for component in target.parts:
                following = self._open_directory_component(current, component)
                self._close_handle(current)
                current = following
            return current
        except BaseException:
            self._close_handle(current)
            raise

    def open_directory(self, target: WorkspacePath) -> int:
        """Return a CRT descriptor, preserving the cross-platform API contract."""

        handle = self._open_directory_handle(target)
        return self._fd_for_handle(handle, os.O_RDONLY)

    def ensure_runtime_directory(self, target: WorkspacePath) -> None:
        if RUNTIME_INTERNAL_WORKSPACE_COMPONENT not in target.parts:
            raise WorkspaceBoundaryError(
                "Runtime directory must stay inside the internal workspace namespace."
            )
        current = self._duplicate_handle(self.root_handle)
        try:
            for component in target.parts:
                try:
                    following = self._open_directory_component(current, component)
                except FileNotFoundError:
                    try:
                        following = self._open_relative_handle(
                            current,
                            component,
                            access=_FILE_LIST_DIRECTORY
                            | _FILE_TRAVERSE
                            | _FILE_READ_ATTRIBUTES,
                            disposition=_FILE_CREATE,
                            directory=True,
                        )
                    except FileExistsError:
                        following = self._open_directory_component(current, component)
                self._close_handle(current)
                current = following
        finally:
            self._close_handle(current)

    @staticmethod
    def _read_bytes_fd(fd: int) -> bytes:
        os.lseek(fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)

    @classmethod
    def _read_fd(cls, fd: int) -> str:
        return cls._read_bytes_fd(fd).decode("utf-8", errors="replace")

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

    @classmethod
    def _fd_for_handle(cls, handle: int, flags: int) -> int:
        try:
            return int(
                msvcrt.open_osfhandle(
                    handle,
                    flags | getattr(os, "O_BINARY", 0),
                )
            )
        except BaseException:
            # Ownership transfers to the CRT only after open_osfhandle succeeds.
            cls._close_handle(handle)
            raise

    @staticmethod
    def _handle_for_fd(fd: int) -> int:
        return int(msvcrt.get_osfhandle(fd))

    @classmethod
    def _open_file_handle(
        cls,
        parent: int,
        name: str,
        *,
        flags: int,
        delete_access: bool = False,
    ) -> int:
        writable = bool(flags & (os.O_WRONLY | os.O_RDWR))
        readable = not bool(flags & os.O_WRONLY) or bool(flags & os.O_RDWR)
        access = _FILE_READ_ATTRIBUTES
        if readable:
            access |= _FILE_READ_DATA
        if writable:
            access |= _FILE_APPEND_DATA if flags & os.O_APPEND else _FILE_WRITE_DATA
        if delete_access:
            access |= _DELETE
        if flags & os.O_CREAT:
            if flags & os.O_EXCL:
                disposition = _FILE_CREATE
            elif flags & os.O_TRUNC:
                disposition = _FILE_OVERWRITE_IF
            else:
                disposition = _FILE_OPEN_IF
        elif flags & os.O_TRUNC:
            disposition = _FILE_OVERWRITE
        else:
            disposition = _FILE_OPEN
        return cls._open_relative_handle(
            parent,
            name,
            access=access,
            disposition=disposition,
            directory=False,
        )

    def _open_file(self, target: WorkspacePath, flags: int) -> int:
        with self._open_parent(target) as (parent, name):
            handle = self._open_file_handle(parent, name, flags=flags)
        return self._fd_for_handle(handle, flags)

    @classmethod
    def _set_rename(
        cls, source_handle: int, target_parent: int, target_name: str
    ) -> None:
        _validate_component(target_name)
        encoded = target_name.encode("utf-16-le")
        size = _FILE_RENAME_INFO.FileName.offset + len(encoded)
        buffer = ctypes.create_string_buffer(size)
        info = ctypes.cast(buffer, ctypes.POINTER(_FILE_RENAME_INFO)).contents
        info.ReplaceIfExists = 1
        info.RootDirectory = wintypes.HANDLE(target_parent)
        info.FileNameLength = len(encoded)
        ctypes.memmove(
            ctypes.addressof(buffer) + _FILE_RENAME_INFO.FileName.offset,
            encoded,
            len(encoded),
        )
        if not _kernel32.SetFileInformationByHandle(
            wintypes.HANDLE(source_handle),
            _FILE_RENAME_INFO_CLASS,
            buffer,
            size,
        ):
            raise ctypes.WinError(ctypes.get_last_error(), target_name)

    @classmethod
    def _set_delete(cls, handle: int) -> None:
        info = _FILE_DISPOSITION_INFO(DeleteFile=True)
        if not _kernel32.SetFileInformationByHandle(
            wintypes.HANDLE(handle),
            _FILE_DISPOSITION_INFO_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def _atomic_replace_text(
        self,
        parent: int,
        name: str,
        content: str,
        *,
        mode: int,
        preserve_mode: bool,
        keep_receipt_fd: bool = False,
    ) -> tuple[int, tuple[int, int, int], int | None]:
        del mode, preserve_mode  # Windows ACLs are inherited from the parent.
        temp_name = ""
        temp_fd: int | None = None
        try:
            for _ in range(32):
                temp_name = f"{_INTERNAL_TEMP_PREFIX}{secrets.token_hex(12)}.tmp"
                try:
                    handle = self._open_file_handle(
                        parent,
                        temp_name,
                        flags=os.O_RDWR | os.O_CREAT | os.O_EXCL,
                        delete_access=True,
                    )
                    temp_fd = self._fd_for_handle(handle, os.O_RDWR)
                    break
                except FileExistsError:
                    continue
            if temp_fd is None:
                raise FileExistsError(
                    "Unable to allocate a unique temporary workspace file"
                )
            written = self._write_fd(temp_fd, content)
            os.fsync(temp_fd)
            identity = self._file_identity(self._handle_for_fd(temp_fd))
            self._set_rename(self._handle_for_fd(temp_fd), parent, name)
            temp_name = ""
            receipt: int | None = None
            if keep_receipt_fd:
                receipt = temp_fd
                temp_fd = None
            else:
                os.close(temp_fd)
                temp_fd = None
            return written, identity, receipt
        finally:
            if temp_fd is not None:
                os.close(temp_fd)
            if temp_name:
                with contextlib.suppress(FileNotFoundError, PermissionError, OSError):
                    handle = self._open_file_handle(
                        parent,
                        temp_name,
                        flags=os.O_RDONLY,
                        delete_access=True,
                    )
                    try:
                        self._set_delete(handle)
                    finally:
                        self._close_handle(handle)

    def read_text(self, target: WorkspacePath) -> str:
        fd = self._open_file(target, os.O_RDONLY)
        try:
            return self._read_fd(fd)
        finally:
            os.close(fd)

    def read_bytes(self, target: WorkspacePath) -> bytes:
        fd = self._open_file(target, os.O_RDONLY)
        try:
            return self._read_bytes_fd(fd)
        finally:
            os.close(fd)

    def write_text(
        self,
        target: WorkspacePath,
        content: str,
        *,
        create_dirs: bool,
    ) -> tuple[str, bool, int]:
        with self._open_parent(target, create_dirs=create_dirs) as (parent, name):
            created = False
            try:
                handle = self._open_file_handle(parent, name, flags=os.O_RDONLY)
            except FileNotFoundError:
                before = ""
                created = True
            else:
                fd = self._fd_for_handle(handle, os.O_RDONLY)
                try:
                    before = self._read_fd(fd)
                finally:
                    os.close(fd)
            written, _, _ = self._atomic_replace_text(
                parent,
                name,
                content,
                mode=0o666,
                preserve_mode=not created,
            )
            return before, created, written

    def write_runtime_text(
        self,
        target: WorkspacePath,
        content: str,
        *,
        create_dirs: bool,
    ) -> int:
        if RUNTIME_INTERNAL_WORKSPACE_COMPONENT not in target.parts:
            raise WorkspaceBoundaryError(
                "Runtime output must stay inside the internal workspace namespace."
            )
        with self._open_parent(target, create_dirs=create_dirs) as (parent, name):
            expected_parent = self._identity(parent)
            written, expected, receipt_fd = self._atomic_replace_text(
                parent,
                name,
                content,
                mode=0o600,
                preserve_mode=False,
                keep_receipt_fd=True,
            )
            assert receipt_fd is not None
            try:
                try:
                    with self._open_parent(target) as (current_parent, current_name):
                        still_named = bool(
                            current_name == name
                            and self._identity(current_parent) == expected_parent
                        )
                except (FileNotFoundError, NotADirectoryError, WorkspaceBoundaryError):
                    still_named = False
                if not still_named:
                    with contextlib.suppress(OSError):
                        self._set_delete(self._handle_for_fd(receipt_fd))
                    raise WorkspaceBoundaryError(
                        "Runtime output parent changed during the filesystem effect."
                    )
                current_fd = self._open_file(target, os.O_RDONLY)
                try:
                    current = self._file_identity(self._handle_for_fd(current_fd))
                finally:
                    os.close(current_fd)
                receipt = self._file_identity(self._handle_for_fd(receipt_fd))
                if current != expected or receipt != expected:
                    raise WorkspaceBoundaryError(
                        "Runtime output target changed during the filesystem effect."
                    )
            finally:
                os.close(receipt_fd)
            return written

    @contextmanager
    def open_runtime_append(
        self,
        target: WorkspacePath,
        *,
        create: bool,
    ) -> Iterator[int]:
        if RUNTIME_INTERNAL_WORKSPACE_COMPONENT not in target.parts:
            raise WorkspaceBoundaryError(
                "Runtime append must stay inside the internal workspace namespace."
            )
        with self._open_parent(target) as (parent, name):
            expected_parent = self._identity(parent)
            flags = os.O_WRONLY | os.O_APPEND | (os.O_CREAT if create else 0)
            handle = self._open_file_handle(parent, name, flags=flags)
            fd = self._fd_for_handle(handle, flags)
            try:
                yield fd
                appended = self._file_identity(self._handle_for_fd(fd))
                with self._open_parent(target) as (current_parent, current_name):
                    if not (
                        current_name == name
                        and self._identity(current_parent) == expected_parent
                    ):
                        raise WorkspaceBoundaryError(
                            "Runtime append parent changed during the filesystem effect."
                        )
                current_fd = self._open_file(target, os.O_RDONLY)
                try:
                    current = self._file_identity(self._handle_for_fd(current_fd))
                finally:
                    os.close(current_fd)
                if current != appended:
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
        with self._open_parent(target) as (parent, name):
            handle = self._open_file_handle(parent, name, flags=os.O_RDONLY)
            fd = self._fd_for_handle(handle, os.O_RDONLY)
            try:
                before = self._read_fd(fd)
            finally:
                os.close(fd)
            after = transform(before)
            written, _, _ = self._atomic_replace_text(
                parent,
                name,
                after,
                mode=0o666,
                preserve_mode=True,
            )
            return before, after, written

    def unlink(self, target: WorkspacePath) -> None:
        with self._open_parent(target) as (parent, name):
            handle = self._open_file_handle(
                parent,
                name,
                flags=os.O_RDONLY,
                delete_access=True,
            )
            try:
                self._set_delete(handle)
            finally:
                self._close_handle(handle)

    def rename(self, source: WorkspacePath, target: WorkspacePath) -> None:
        with self._open_parent(source) as (source_parent, source_name):
            source_parent_identity = self._identity(source_parent)
            source_handle = self._open_file_handle(
                source_parent,
                source_name,
                flags=os.O_RDONLY,
                delete_access=True,
            )
            try:
                source_identity = self._file_identity(source_handle)
                with self._open_parent(target, create_dirs=True) as (
                    target_parent,
                    target_name,
                ):
                    target_parent_identity = self._identity(target_parent)
                    try:
                        existing = self._open_file_handle(
                            target_parent, target_name, flags=os.O_RDONLY
                        )
                    except FileNotFoundError:
                        existing = None
                    if existing is not None:
                        self._close_handle(existing)
                    self._set_rename(source_handle, target_parent, target_name)
                    try:
                        with (
                            self._open_parent(source) as (
                                current_source_parent,
                                current_source_name,
                            ),
                            self._open_parent(target) as (
                                current_target_parent,
                                current_target_name,
                            ),
                        ):
                            parents_still_named = bool(
                                current_source_name == source_name
                                and current_target_name == target_name
                                and self._identity(current_source_parent)
                                == source_parent_identity
                                and self._identity(current_target_parent)
                                == target_parent_identity
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
                        current_identity = self._file_identity(
                            self._handle_for_fd(current_fd)
                        )
                    finally:
                        os.close(current_fd)
                    if current_identity != source_identity:
                        raise WorkspaceBoundaryError(
                            "Runtime rename target changed during the filesystem effect."
                        )
            finally:
                self._close_handle(source_handle)

    @classmethod
    def _directory_rows(cls, handle: int) -> Iterator[tuple[str, int, int]]:
        restart = True
        while True:
            buffer = ctypes.create_string_buffer(64 * 1024)
            io_status = _IO_STATUS_BLOCK()
            status = int(
                _ntdll.NtQueryDirectoryFile(
                    wintypes.HANDLE(handle),
                    None,
                    None,
                    None,
                    ctypes.byref(io_status),
                    buffer,
                    len(buffer),
                    _FILE_DIRECTORY_INFORMATION_CLASS,
                    False,
                    None,
                    restart,
                )
            )
            restart = False
            unsigned = ctypes.c_ulong(status).value
            if unsigned == _STATUS_NO_MORE_FILES:
                return
            if status < 0 and unsigned != _STATUS_BUFFER_OVERFLOW:
                cls._raise_status(status, "<directory enumeration>")
            used = int(io_status.Information)
            offset = 0
            while used and offset < used:
                address = ctypes.addressof(buffer) + offset
                row = ctypes.cast(
                    address, ctypes.POINTER(_FILE_DIRECTORY_INFORMATION)
                ).contents
                name = ctypes.wstring_at(
                    address + _FILE_DIRECTORY_INFORMATION.FileName.offset,
                    int(row.FileNameLength) // ctypes.sizeof(wintypes.WCHAR),
                )
                if name not in {".", ".."}:
                    yield name, int(row.FileAttributes), int(row.EndOfFile)
                if not row.NextEntryOffset:
                    break
                offset += int(row.NextEntryOffset)

    def iter_entries(
        self,
        target: WorkspacePath,
        *,
        recursive: bool,
        max_depth: int | None = None,
    ) -> Iterator[WorkspaceEntry]:
        directory = self._open_directory_handle(target)

        def _walk(
            current: int,
            prefix: tuple[str, ...],
            depth: int,
        ) -> Iterator[WorkspaceEntry]:
            try:
                rows = sorted(
                    self._directory_rows(current), key=lambda row: row[0].lower()
                )
            except (FileNotFoundError, NotADirectoryError, PermissionError):
                return
            for name, attributes, size in rows:
                if _reserved_internal_component(name):
                    continue
                if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                    continue
                is_dir = bool(attributes & _FILE_ATTRIBUTE_DIRECTORY)
                parts = (*prefix, name)
                yield WorkspaceEntry(
                    parts=parts,
                    relative_path=str(Path(*parts)),
                    is_dir=is_dir,
                    size=0 if is_dir else size,
                )
                if not recursive or not is_dir:
                    continue
                if max_depth is not None and depth >= max_depth:
                    continue
                try:
                    child = self._open_directory_component(current, name)
                except (
                    FileNotFoundError,
                    NotADirectoryError,
                    PermissionError,
                    WorkspaceBoundaryError,
                ):
                    continue
                try:
                    yield from _walk(child, parts, depth + 1)
                finally:
                    self._close_handle(child)

        try:
            yield from _walk(directory, (), 0)
        finally:
            self._close_handle(directory)

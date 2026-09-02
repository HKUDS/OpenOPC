"""Small cross-platform advisory file-lock capability."""

from __future__ import annotations

import contextlib
import ctypes
import os
from contextlib import contextmanager
from typing import Iterator


if os.name == "posix":
    import fcntl
else:  # pragma: posix cover
    fcntl = None  # type: ignore[assignment]

if os.name == "nt":  # pragma: win32 cover
    import msvcrt
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
    _LOCKFILE_EXCLUSIVE_LOCK = 0x00000002

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _kernel32.LockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_OVERLAPPED),
    ]
    _kernel32.LockFileEx.restype = wintypes.BOOL
    _kernel32.UnlockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_OVERLAPPED),
    ]
    _kernel32.UnlockFileEx.restype = wintypes.BOOL
else:  # pragma: win32 cover
    msvcrt = None  # type: ignore[assignment]
    _kernel32 = None


def lock_file_descriptor(fd: int, *, blocking: bool = True) -> None:
    """Acquire an exclusive process lock that is released when ``fd`` closes."""

    if os.name == "posix":
        operation = fcntl.LOCK_EX
        if not blocking:
            operation |= fcntl.LOCK_NB
        fcntl.flock(fd, operation)
        return
    if os.name == "nt":  # pragma: win32 cover
        flags = _LOCKFILE_EXCLUSIVE_LOCK
        if not blocking:
            flags |= _LOCKFILE_FAIL_IMMEDIATELY
        overlapped = _OVERLAPPED()
        handle = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
        if not _kernel32.LockFileEx(
            handle,
            flags,
            0,
            0xFFFFFFFF,
            0xFFFFFFFF,
            ctypes.byref(overlapped),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return
    raise OSError("Cross-platform file locking is unavailable on this platform")


def unlock_file_descriptor(fd: int) -> None:
    if os.name == "posix":
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    if os.name == "nt":  # pragma: win32 cover
        overlapped = _OVERLAPPED()
        handle = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
        if not _kernel32.UnlockFileEx(
            handle,
            0,
            0xFFFFFFFF,
            0xFFFFFFFF,
            ctypes.byref(overlapped),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return
    raise OSError("Cross-platform file locking is unavailable on this platform")


@contextmanager
def exclusive_file_lock(fd: int, *, blocking: bool = True) -> Iterator[None]:
    lock_file_descriptor(fd, blocking=blocking)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            unlock_file_descriptor(fd)

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from opc.core.file_lock import (
    exclusive_file_lock,
    lock_file_descriptor,
    unlock_file_descriptor,
)
from opc.plugins.office_ui.server import _acquire_single_instance_lock


class CrossPlatformFileLockTests(unittest.TestCase):
    def test_exclusive_lock_rejects_second_process_handle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runtime.lock"
            first = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            second = os.open(path, os.O_RDWR)
            try:
                lock_file_descriptor(first, blocking=False)
                with self.assertRaises(OSError):
                    lock_file_descriptor(second, blocking=False)
                unlock_file_descriptor(first)
                lock_file_descriptor(second, blocking=False)
                unlock_file_descriptor(second)
            finally:
                os.close(second)
                os.close(first)

    def test_context_manager_releases_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runtime.lock"
            first = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            second = os.open(path, os.O_RDWR)
            try:
                with exclusive_file_lock(first, blocking=False):
                    with self.assertRaises(OSError):
                        lock_file_descriptor(second, blocking=False)
                lock_file_descriptor(second, blocking=False)
                unlock_file_descriptor(second)
            finally:
                os.close(second)
                os.close(first)

    def test_office_ui_single_instance_guard_is_cross_platform(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            opc_home = Path(temp_dir)
            first = _acquire_single_instance_lock(opc_home)
            try:
                with self.assertRaises(SystemExit):
                    _acquire_single_instance_lock(opc_home)
            finally:
                first.close()
            replacement = _acquire_single_instance_lock(opc_home)
            replacement.close()


if __name__ == "__main__":
    unittest.main()

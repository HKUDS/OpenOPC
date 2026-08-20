from __future__ import annotations

import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from opc.core.models import Task, TaskStatus
from opc.layer2_organization import comms as file_comms
from opc.layer3_agent.runtime_v2.permissions import RuntimePermissionAdapter
from opc.layer3_agent.runtime_v2.runtime import NativeRuntimeV2
from opc.layer3_agent.runtime_v2.streaming_tool_executor import StreamingToolExecutor
from opc.layer3_agent.runtime_v2.tool_planner import ToolPlanner
from opc.layer4_tools.file_ops import (
    apply_patch,
    create_file_tools,
    file_edit,
    file_read,
    file_write,
)
from opc.layer4_tools.output_budget import persist_tool_result
from opc.layer4_tools.registry import ToolRegistry
from opc.layer4_tools.workspace_fs import SecureWorkspace, WorkspaceBoundaryError


class NativeFileWorkspaceBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def _executor(self) -> StreamingToolExecutor:
        registry = ToolRegistry()
        for tool in create_file_tools():
            registry.register(tool)
        return StreamingToolExecutor(
            registry=registry,
            planner=ToolPlanner(registry),
            permission_resolver=RuntimePermissionAdapter(),
        )

    @staticmethod
    def _task(workspace: Path) -> Task:
        resolved = str(workspace.resolve())
        return Task(
            id="company-file-boundary",
            session_id="role-session",
            project_id="project-one",
            metadata={
                "execution_mode": "task_mode",
                "workspace_root": resolved,
                "output_root": resolved,
                "_execution_context": {
                    "workspace_root": resolved,
                    "output_root": resolved,
                    "comms_root": str(workspace.resolve() / ".opc-comms"),
                },
            },
        )

    async def _call(
        self,
        executor: StreamingToolExecutor,
        task: Task,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        results = await executor.execute(
            [{"id": f"call-{name}", "function": name, "arguments": arguments}],
            task=task,
        )
        return dict(results[0]["result"])

    def _assert_workspace_rejection(self, result: dict[str, Any]) -> None:
        self.assertTrue(result.get("success"), result)
        payload = dict(result.get("result", {}) or {})
        self.assertFalse(payload.get("success", True), result)
        self.assertIn("workspace boundary", str(payload.get("error", "")).lower())

    async def _call_while_swapping_child_to_symlink(
        self,
        *,
        workspace: Path,
        sibling: Path,
        task: Task,
        tool_name: str,
        arguments: dict[str, Any],
        swap_after_open: bool = False,
    ) -> dict[str, Any]:
        ready = threading.Event()
        swapped = threading.Event()
        swap_errors: list[BaseException] = []
        original = SecureWorkspace._open_directory_component

        def _barrier_open(
            secure_workspace: SecureWorkspace,
            parent_fd: int,
            component: str,
        ) -> int:
            if component == "slot" and not ready.is_set():
                opened_fd = (
                    original(secure_workspace, parent_fd, component)
                    if swap_after_open
                    else None
                )
                ready.set()
                if not swapped.wait(timeout=5):
                    if opened_fd is not None:
                        os.close(opened_fd)
                    raise AssertionError("filesystem swap barrier timed out")
                if opened_fd is not None:
                    return opened_fd
            return original(secure_workspace, parent_fd, component)

        def _swap() -> None:
            try:
                if not ready.wait(timeout=5):
                    raise AssertionError("tool did not reach filesystem swap barrier")
                os.rename(workspace / "slot", workspace / "slot-before-swap")
                (workspace / "slot").symlink_to(sibling, target_is_directory=True)
            except BaseException as exc:  # pragma: no cover - asserted below
                swap_errors.append(exc)
            finally:
                swapped.set()

        swapper = threading.Thread(target=_swap, daemon=True)
        swapper.start()
        with patch.object(
            SecureWorkspace,
            "_open_directory_component",
            new=_barrier_open,
        ):
            result = await self._call(
                self._executor(),
                task,
                tool_name,
                arguments,
            )
        swapper.join(timeout=5)
        self.assertFalse(swapper.is_alive())
        self.assertEqual(swap_errors, [])
        return result

    async def test_runtime_file_calls_allow_paths_inside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            workspace.mkdir()
            task = self._task(workspace)
            executor = self._executor()

            write_result = await self._call(
                executor,
                task,
                "file_write",
                {"path": "src/demo.txt", "content": "alpha\n"},
            )
            read_result = await self._call(
                executor,
                task,
                "file_read",
                {"path": str(workspace / "src" / "demo.txt")},
            )
            edit_result = await self._call(
                executor,
                task,
                "file_edit",
                {
                    "path": "src/demo.txt",
                    "old_string": "alpha",
                    "new_string": "beta",
                },
            )
            list_result = await self._call(
                executor,
                task,
                "list_dir",
                {"path": "src"},
            )
            search_result = await self._call(
                executor,
                task,
                "file_search",
                {"pattern": "beta", "directory": "src"},
            )

            for result in (
                write_result,
                read_result,
                edit_result,
                list_result,
                search_result,
            ):
                self.assertTrue(result["success"], result)
                self.assertTrue(result["result"]["success"], result)
            self.assertEqual((workspace / "src" / "demo.txt").read_text(), "beta\n")

    async def test_runtime_internal_results_are_exactly_readable_but_never_listed_or_mutated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            executor = self._executor()
            for index, company in enumerate((False, True), 1):
                task = self._task(workspace)
                task.id = f"runtime-result-{index}"
                if company:
                    task.metadata["execution_mode"] = "company_mode"
                persisted = persist_tool_result(
                    f"full result {index}",
                    tool_name="web_fetch",
                    task=task,
                    extension="txt",
                )
                result = await file_read(
                    path=str(persisted["full_output_path"]),
                    task=task,
                )
                self.assertTrue(result["success"], result)
                self.assertEqual(result["content"], f"full result {index}")

            ordinary_task = self._task(workspace)
            listed = await self._call(
                executor,
                ordinary_task,
                "list_dir",
                {"path": ".", "recursive": True},
            )
            self.assertTrue(listed["result"]["success"], listed)
            self.assertNotIn(".opc-comms", str(listed["result"]))

            protected = next(
                (workspace / ".opc-comms").rglob("*.txt")
            )
            original = protected.read_text(encoding="utf-8")
            attempts = (
                await file_write(
                    path=str(protected),
                    content="overwrite",
                    task=ordinary_task,
                ),
                await file_edit(
                    path=str(protected),
                    old_string="full",
                    new_string="changed",
                    task=ordinary_task,
                ),
                await apply_patch(
                    patch="\n".join(
                        (
                            "*** Begin Patch",
                            f"*** Delete File: {protected}",
                            "*** End Patch",
                        )
                    ),
                    task=ordinary_task,
                ),
                await apply_patch(
                    patch="\n".join(
                        (
                            "*** Begin Patch",
                            f"*** Update File: {protected}",
                            f"*** Move to: {workspace / 'moved-result.txt'}",
                            "@@",
                            f"-{original}",
                            f"+{original}",
                            "*** End Patch",
                        )
                    ),
                    task=ordinary_task,
                ),
            )
            for result in attempts:
                self.assertFalse(result["success"], result)
                self.assertIn("runtime-internal", str(result["error"]))
            self.assertEqual(protected.read_text(encoding="utf-8"), original)
            self.assertFalse((workspace / "moved-result.txt").exists())

    async def test_runtime_internal_writer_rejects_preexisting_and_concurrent_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            external = root / "external"
            workspace.mkdir()
            external.mkdir()
            task = self._task(workspace)
            comms = workspace / ".opc-comms"
            comms.symlink_to(external, target_is_directory=True)

            with self.assertRaises(WorkspaceBoundaryError):
                persist_tool_result(
                    "must not escape",
                    tool_name="web_fetch",
                    task=task,
                    extension="txt",
                )
            self.assertEqual(list(external.iterdir()), [])

            comms.unlink()
            comms.mkdir()
            ready = threading.Event()
            swapped = threading.Event()
            original_open = SecureWorkspace._open_directory_component

            def _swap_barrier(
                secure_workspace: SecureWorkspace,
                parent_fd: int,
                component: str,
            ) -> int:
                opened = original_open(secure_workspace, parent_fd, component)
                if component == ".opc-comms" and not ready.is_set():
                    ready.set()
                    if not swapped.wait(timeout=5):
                        os.close(opened)
                        raise AssertionError("runtime comms swap barrier timed out")
                return opened

            def _swap() -> None:
                if not ready.wait(timeout=5):
                    return
                os.rename(comms, workspace / ".opc-comms-before-swap")
                comms.symlink_to(external, target_is_directory=True)
                swapped.set()

            swapper = threading.Thread(target=_swap, daemon=True)
            swapper.start()
            with patch.object(
                SecureWorkspace,
                "_open_directory_component",
                new=_swap_barrier,
            ), self.assertRaises(WorkspaceBoundaryError):
                persist_tool_result(
                    "must remain pinned",
                    tool_name="python_exec",
                    task=task,
                    extension="txt",
                )
            swapper.join(timeout=5)
            self.assertFalse(swapper.is_alive())
            self.assertEqual(list(external.iterdir()), [])

    async def test_runtime_internal_writer_rejects_same_parent_target_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            (workspace / ".opc-comms").mkdir()
            task = self._task(workspace)
            original_atomic = SecureWorkspace._atomic_replace_text

            def _replace_target_after_atomic(
                secure_workspace: SecureWorkspace,
                parent_fd: int,
                name: str,
                content: str,
                *,
                mode: int,
                preserve_mode: bool,
                keep_receipt_fd: bool = False,
            ) -> tuple[int, tuple[int, int, int], int | None]:
                receipt = original_atomic(
                    secure_workspace,
                    parent_fd,
                    name,
                    content,
                    mode=mode,
                    preserve_mode=preserve_mode,
                    keep_receipt_fd=keep_receipt_fd,
                )
                os.rename(
                    name,
                    f"captured-{name}",
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                replacement_fd = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    mode=0o600,
                    dir_fd=parent_fd,
                )
                try:
                    os.write(replacement_fd, b"foreign replacement")
                finally:
                    os.close(replacement_fd)
                return receipt

            with patch.object(
                SecureWorkspace,
                "_atomic_replace_text",
                new=_replace_target_after_atomic,
            ), self.assertRaises(WorkspaceBoundaryError):
                persist_tool_result(
                    "approved payload",
                    tool_name="web_fetch",
                    task=task,
                    extension="txt",
                )

    async def test_runtime_internal_write_and_rename_require_durable_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_root = Path(temp_dir) / "workspace"
            workspace_root.mkdir()
            secure = SecureWorkspace(
                str(workspace_root),
                str(workspace_root),
            )
            with secure:
                internal = secure.resolve(
                    str(workspace_root / ".opc-comms"),
                    use_output_root=False,
                    allow_runtime_internal_read=True,
                )
                secure.ensure_runtime_directory(internal)
                source_dir = secure.resolve(
                    str(workspace_root / ".opc-comms" / "source"),
                    use_output_root=False,
                    allow_runtime_internal_read=True,
                )
                target_dir = secure.resolve(
                    str(workspace_root / ".opc-comms" / "target"),
                    use_output_root=False,
                    allow_runtime_internal_read=True,
                )
                secure.ensure_runtime_directory(source_dir)
                secure.ensure_runtime_directory(target_dir)
                source = secure.resolve(
                    str(source_dir.display_path / "message.md"),
                    use_output_root=False,
                    allow_runtime_internal_read=True,
                )
                target = secure.resolve(
                    str(target_dir.display_path / "message.md"),
                    use_output_root=False,
                    allow_runtime_internal_read=True,
                )

                original_fsync = os.fsync
                sync_kinds: list[str] = []

                def _trace_fsync(fd: int) -> None:
                    mode = os.fstat(fd).st_mode
                    sync_kinds.append(
                        "dir" if stat.S_ISDIR(mode) else "file"
                    )
                    original_fsync(fd)

                with patch(
                    "opc.layer4_tools.workspace_fs.os.fsync",
                    side_effect=_trace_fsync,
                ):
                    secure.write_runtime_text(
                        source,
                        "durable",
                        create_dirs=False,
                    )
                self.assertEqual(sync_kinds[-2:], ["file", "dir"])

                sync_kinds.clear()
                with patch(
                    "opc.layer4_tools.workspace_fs.os.fsync",
                    side_effect=_trace_fsync,
                ):
                    secure.rename(source, target)
                self.assertEqual(sync_kinds, ["dir", "dir"])

                failed_target = secure.resolve(
                    str(target_dir.display_path / "failed.txt"),
                    use_output_root=False,
                    allow_runtime_internal_read=True,
                )

                def _fail_directory_sync(fd: int) -> None:
                    if stat.S_ISDIR(os.fstat(fd).st_mode):
                        raise OSError("simulated directory fsync failure")
                    original_fsync(fd)

                with patch(
                    "opc.layer4_tools.workspace_fs.os.fsync",
                    side_effect=_fail_directory_sync,
                ), self.assertRaisesRegex(OSError, "directory fsync failure"):
                    secure.write_runtime_text(
                        failed_target,
                        "not acknowledged",
                        create_dirs=False,
                    )

                rename_failure_source = secure.resolve(
                    str(source_dir.display_path / "rename-failure.md"),
                    use_output_root=False,
                    allow_runtime_internal_read=True,
                )
                rename_failure_target = secure.resolve(
                    str(target_dir.display_path / "rename-failure.md"),
                    use_output_root=False,
                    allow_runtime_internal_read=True,
                )
                secure.write_runtime_text(
                    rename_failure_source,
                    "not acknowledged",
                    create_dirs=False,
                )
                with patch(
                    "opc.layer4_tools.workspace_fs.os.fsync",
                    side_effect=_fail_directory_sync,
                ), self.assertRaisesRegex(OSError, "directory fsync failure"):
                    secure.rename(
                        rename_failure_source,
                        rename_failure_target,
                    )

    async def test_comms_delivery_and_seen_rename_reject_parent_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_root = Path(temp_dir) / "workspace"
            workspace_root.mkdir()
            layout = file_comms.resolve_layout(
                workspace_root,
                "project-one",
                "session-one",
            )
            file_comms.ensure_layout(layout, ["sender", "receiver"])
            original_rename = os.rename

            def _swap_after_cross_directory_rename(
                replaced_dir: Path,
                backup_dir: Path,
            ):
                swapped = False

                def _rename(source, target, *args, **kwargs):
                    nonlocal swapped
                    result = original_rename(source, target, *args, **kwargs)
                    if (
                        not swapped
                        and kwargs.get("src_dir_fd") is not None
                        and kwargs.get("dst_dir_fd") is not None
                        and kwargs.get("src_dir_fd")
                        != kwargs.get("dst_dir_fd")
                    ):
                        swapped = True
                        original_rename(replaced_dir, backup_dir)
                        replaced_dir.mkdir()
                    return result

                return _rename

            new_dir = layout.role_new_dir("receiver")
            with patch(
                "opc.layer4_tools.workspace_fs.os.rename",
                side_effect=_swap_after_cross_directory_rename(
                    new_dir,
                    new_dir.with_name("new-before-swap"),
                ),
            ), self.assertRaises(WorkspaceBoundaryError):
                file_comms.send_message(
                    layout,
                    from_role="sender",
                    to_role="receiver",
                    subject="must fail receipt",
                    body="delivery",
                )
            self.assertEqual(list(new_dir.iterdir()), [])

            delivered = file_comms.send_message(
                layout,
                from_role="sender",
                to_role="receiver",
                subject="mark seen",
                body="archive",
            )
            seen_dir = layout.role_seen_dir("receiver")
            with patch(
                "opc.layer4_tools.workspace_fs.os.rename",
                side_effect=_swap_after_cross_directory_rename(
                    seen_dir,
                    seen_dir.with_name("seen-before-swap"),
                ),
            ), self.assertRaises(WorkspaceBoundaryError):
                file_comms.mark_seen(layout, "receiver", [delivered])
            self.assertEqual(list(seen_dir.iterdir()), [])

    async def test_model_workspace_capability_never_renames_or_deletes_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_root = Path(temp_dir) / "workspace"
            workspace_root.mkdir()
            protected_dir = workspace_root / "directory"
            protected_dir.mkdir()
            with SecureWorkspace(
                str(workspace_root),
                str(workspace_root),
            ) as secure:
                source = secure.resolve(
                    str(protected_dir),
                    use_output_root=False,
                )
                target = secure.resolve(
                    str(workspace_root / "moved-directory"),
                    use_output_root=False,
                )
                with self.assertRaises(WorkspaceBoundaryError):
                    secure.unlink(source)
                with self.assertRaises(WorkspaceBoundaryError):
                    secure.rename(source, target)
                with self.assertRaises(WorkspaceBoundaryError):
                    secure.resolve("/usr/bin/bash", use_output_root=False)

    async def test_runtime_file_calls_reject_absolute_and_parent_escape_before_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            sibling = root / "sibling"
            workspace.mkdir()
            sibling.mkdir()
            outside = sibling / "outside.txt"
            outside.write_text("outside\n", encoding="utf-8")
            task = self._task(workspace)
            executor = self._executor()

            attempts = [
                ("file_write", {"path": str(sibling / "absolute.txt"), "content": "bad"}),
                ("file_write", {"path": "../parent.txt", "content": "bad"}),
                ("file_read", {"path": str(outside)}),
                (
                    "file_edit",
                    {"path": str(outside), "old_string": "outside", "new_string": "changed"},
                ),
                ("list_dir", {"path": str(sibling)}),
                ("file_search", {"pattern": "outside", "directory": str(sibling)}),
                ("grep", {"query": "outside", "path": str(sibling)}),
                ("glob", {"pattern": "*.txt", "path": str(sibling)}),
            ]
            for name, arguments in attempts:
                with self.subTest(tool=name, arguments=arguments):
                    result = await self._call(executor, task, name, arguments)
                    self._assert_workspace_rejection(result)

            mismatched_output_task = self._task(workspace)
            mismatched_output_task.metadata["output_root"] = str(sibling)
            mismatched_output_task.metadata["_execution_context"]["output_root"] = str(
                sibling
            )
            mismatched_output_result = await self._call(
                executor,
                mismatched_output_task,
                "file_write",
                {"path": "relative-via-output-root.txt", "content": "bad"},
            )
            self._assert_workspace_rejection(mismatched_output_result)

            self.assertFalse((sibling / "absolute.txt").exists())
            self.assertFalse((sibling / "relative-via-output-root.txt").exists())
            self.assertFalse((root / "parent.txt").exists())
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")

    async def test_streaming_runtime_arguments_cannot_replace_trusted_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            sibling = root / "sibling"
            workspace.mkdir()
            sibling.mkdir()
            task = self._task(workspace)
            executor = self._executor()

            attempts = (
                {"task": None},
                {
                    "task": {
                        "metadata": {
                            "workspace_root": str(sibling),
                            "output_root": str(sibling),
                        }
                    }
                },
                {"on_progress": None},
            )
            for index, injected in enumerate(attempts):
                target = sibling / f"reserved-context-{index}.txt"
                with self.subTest(injected=injected):
                    result = await self._call(
                        executor,
                        task,
                        "file_write",
                        {
                            "path": str(target),
                            "content": "must-not-exist",
                            **injected,
                        },
                    )
                    self.assertFalse(target.exists(), result)
                    payload = (
                        dict(result.get("result", {}) or {})
                        if result.get("success", True)
                        else result
                    )
                    self.assertFalse(payload.get("success", True), result)
                    self.assertTrue(
                        "workspace boundary" in str(payload.get("error", "")).lower()
                        or "reserved runtime argument" in str(payload.get("error", "")).lower(),
                        result,
                    )

    async def test_runtime_file_calls_reject_symlink_escape_including_missing_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            sibling = root / "sibling"
            workspace.mkdir()
            sibling.mkdir()
            outside = sibling / "existing.txt"
            outside.write_text("outside\n", encoding="utf-8")
            (workspace / "escape").symlink_to(sibling, target_is_directory=True)
            task = self._task(workspace)
            executor = self._executor()

            attempts = [
                ("file_write", {"path": "escape/missing.txt", "content": "bad"}),
                ("file_read", {"path": "escape/existing.txt"}),
                (
                    "file_edit",
                    {"path": "escape/existing.txt", "old_string": "outside", "new_string": "changed"},
                ),
                ("list_dir", {"path": "escape"}),
                ("file_search", {"pattern": "outside", "directory": "escape"}),
            ]
            for name, arguments in attempts:
                with self.subTest(tool=name):
                    result = await self._call(executor, task, name, arguments)
                    self._assert_workspace_rejection(result)

            self.assertFalse((sibling / "missing.txt").exists())
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")

    async def test_runtime_file_effects_reject_real_child_symlink_swap_barrier(self) -> None:
        cases = [
            (
                "file_write",
                {"path": "slot/effect.txt", "content": "bad"},
                False,
            ),
            ("file_read", {"path": "slot/effect.txt"}, True),
            (
                "file_edit",
                {
                    "path": "slot/effect.txt",
                    "old_string": "inside",
                    "new_string": "changed",
                },
                True,
            ),
            (
                "apply_patch",
                {
                    "patch": "\n".join(
                        [
                            "*** Begin Patch",
                            "*** Update File: slot/effect.txt",
                            "@@",
                            "-inside",
                            "+changed",
                            "*** End Patch",
                        ]
                    )
                },
                True,
            ),
            ("list_dir", {"path": "slot"}, True),
            (
                "file_search",
                {"pattern": "inside", "directory": "slot"},
                True,
            ),
            ("glob", {"pattern": "*.txt", "path": "slot"}, True),
        ]
        for tool_name, arguments, seed_file in cases:
            with self.subTest(tool=tool_name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                workspace = root / "workspace"
                sibling = root / "sibling"
                slot = workspace / "slot"
                slot.mkdir(parents=True)
                sibling.mkdir()
                if seed_file:
                    (slot / "effect.txt").write_text("inside\n", encoding="utf-8")
                    (sibling / "effect.txt").write_text("outside\n", encoding="utf-8")

                result = await self._call_while_swapping_child_to_symlink(
                    workspace=workspace,
                    sibling=sibling,
                    task=self._task(workspace),
                    tool_name=tool_name,
                    arguments=arguments,
                )

                self._assert_workspace_rejection(result)
                outside = sibling / "effect.txt"
                if seed_file:
                    self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")
                else:
                    self.assertFalse(outside.exists())

    async def test_scoped_directory_tools_pin_root_across_real_root_swap_barrier(self) -> None:
        cases = [
            ("list_dir", {"path": "."}),
            ("file_search", {"pattern": "needle", "directory": "."}),
            ("glob", {"pattern": "*.txt", "path": "."}),
        ]
        for tool_name, arguments in cases:
            with self.subTest(tool=tool_name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                workspace = root / "workspace"
                sibling = root / "sibling"
                pinned = root / "workspace-before-swap"
                workspace.mkdir()
                sibling.mkdir()
                (workspace / "inside.txt").write_text("needle inside\n", encoding="utf-8")
                (sibling / "outside.txt").write_text("needle outside\n", encoding="utf-8")
                task = self._task(workspace)
                ready = threading.Event()
                swapped = threading.Event()
                swap_errors: list[BaseException] = []
                original = SecureWorkspace.resolve

                def _barrier_resolve(
                    secure_workspace: SecureWorkspace,
                    path: str,
                    *,
                    use_output_root: bool = True,
                ):
                    if not ready.is_set():
                        ready.set()
                        if not swapped.wait(timeout=5):
                            raise AssertionError("root swap barrier timed out")
                    return original(
                        secure_workspace,
                        path,
                        use_output_root=use_output_root,
                    )

                def _swap_root() -> None:
                    try:
                        if not ready.wait(timeout=5):
                            raise AssertionError("tool did not reach root swap barrier")
                        os.rename(workspace, pinned)
                        workspace.symlink_to(sibling, target_is_directory=True)
                    except BaseException as exc:  # pragma: no cover - asserted below
                        swap_errors.append(exc)
                    finally:
                        swapped.set()

                swapper = threading.Thread(target=_swap_root, daemon=True)
                swapper.start()
                with patch.object(SecureWorkspace, "resolve", new=_barrier_resolve):
                    result = await self._call(
                        self._executor(),
                        task,
                        tool_name,
                        arguments,
                    )
                swapper.join(timeout=5)

                self.assertFalse(swapper.is_alive())
                self.assertEqual(swap_errors, [])
                self.assertTrue(result["success"], result)
                payload = dict(result["result"])
                self.assertTrue(payload["success"], payload)
                rendered = str(payload)
                self.assertIn("inside", rendered)
                self.assertNotIn("outside", rendered)

    async def test_file_tools_use_pinned_child_fd_after_real_path_swap(self) -> None:
        cases = [
            ("file_write", {"path": "slot/effect.txt", "content": "written\n"}),
            ("file_read", {"path": "slot/effect.txt"}),
            (
                "file_edit",
                {
                    "path": "slot/effect.txt",
                    "old_string": "inside",
                    "new_string": "changed",
                },
            ),
            (
                "apply_patch",
                {
                    "patch": "\n".join(
                        [
                            "*** Begin Patch",
                            "*** Update File: slot/effect.txt",
                            "@@",
                            "-inside",
                            "+changed",
                            "*** End Patch",
                        ]
                    )
                },
            ),
            ("list_dir", {"path": "slot"}),
            ("file_search", {"pattern": "inside", "directory": "slot"}),
            ("glob", {"pattern": "*.txt", "path": "slot"}),
        ]
        for tool_name, arguments in cases:
            with self.subTest(tool=tool_name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                workspace = root / "workspace"
                sibling = root / "sibling"
                slot = workspace / "slot"
                held_slot = workspace / "slot-before-swap"
                slot.mkdir(parents=True)
                sibling.mkdir()
                (slot / "effect.txt").write_text("inside\n", encoding="utf-8")
                outside = sibling / "effect.txt"
                outside.write_text("outside\n", encoding="utf-8")

                result = await self._call_while_swapping_child_to_symlink(
                    workspace=workspace,
                    sibling=sibling,
                    task=self._task(workspace),
                    tool_name=tool_name,
                    arguments=arguments,
                    swap_after_open=True,
                )

                self.assertTrue(result["success"], result)
                self.assertTrue(result["result"]["success"], result)
                self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")
                if tool_name == "file_write":
                    self.assertEqual(
                        (held_slot / "effect.txt").read_text(encoding="utf-8"),
                        "written\n",
                    )
                elif tool_name in {"file_edit", "apply_patch"}:
                    self.assertEqual(
                        (held_slot / "effect.txt").read_text(encoding="utf-8"),
                        "changed\n",
                    )
                elif tool_name == "file_read":
                    self.assertIn("inside", str(result["result"]))
                if tool_name in {"list_dir", "glob"}:
                    self.assertIn("effect.txt", str(result["result"]))
                    self.assertNotIn("outside", str(result["result"]))

    async def test_apply_patch_add_delete_and_move_use_pinned_child_fds(self) -> None:
        patches = [
            (
                "add",
                "\n".join(
                    [
                        "*** Begin Patch",
                        "*** Add File: slot/added.txt",
                        "+inside",
                        "*** End Patch",
                    ]
                ),
            ),
            (
                "delete",
                "\n".join(
                    [
                        "*** Begin Patch",
                        "*** Delete File: slot/effect.txt",
                        "*** End Patch",
                    ]
                ),
            ),
            (
                "move",
                "\n".join(
                    [
                        "*** Begin Patch",
                        "*** Update File: slot/effect.txt",
                        "*** Move to: slot/moved.txt",
                        "@@",
                        "-inside",
                        "+moved",
                        "*** End Patch",
                    ]
                ),
            ),
        ]
        for kind, patch_text in patches:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                workspace = root / "workspace"
                sibling = root / "sibling"
                slot = workspace / "slot"
                held_slot = workspace / "slot-before-swap"
                slot.mkdir(parents=True)
                sibling.mkdir()
                (slot / "effect.txt").write_text("inside\n", encoding="utf-8")
                outside = sibling / "effect.txt"
                outside.write_text("outside\n", encoding="utf-8")

                result = await self._call_while_swapping_child_to_symlink(
                    workspace=workspace,
                    sibling=sibling,
                    task=self._task(workspace),
                    tool_name="apply_patch",
                    arguments={"patch": patch_text},
                    swap_after_open=True,
                )

                self.assertTrue(result["success"], result)
                self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")
                if not result["result"]["success"]:
                    self._assert_workspace_rejection(result)
                    self.assertFalse((sibling / "added.txt").exists())
                    self.assertFalse((sibling / "moved.txt").exists())
                    continue
                if kind == "add":
                    self.assertEqual(
                        (held_slot / "added.txt").read_text(encoding="utf-8"),
                        "inside",
                    )
                    self.assertFalse((sibling / "added.txt").exists())
                elif kind == "delete":
                    self.assertFalse((held_slot / "effect.txt").exists())
                else:
                    self.assertFalse((held_slot / "effect.txt").exists())
                    self.assertEqual(
                        (held_slot / "moved.txt").read_text(encoding="utf-8"),
                        "moved\n",
                    )
                    self.assertFalse((sibling / "moved.txt").exists())

    async def test_worktree_execution_context_overrides_inherited_parent_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            parent_workspace = root / "parent"
            worktree = root / "worktree"
            parent_workspace.mkdir()
            worktree.mkdir()
            task = self._task(worktree)
            task.metadata["workspace_root"] = str(parent_workspace)
            task.metadata["output_root"] = str(parent_workspace)

            result = await self._call(
                self._executor(),
                task,
                "file_write",
                {"path": "isolated.txt", "content": "worktree\n"},
            )

            self.assertTrue(result["success"], result)
            self.assertTrue(result["result"]["success"], result)
            self.assertEqual(
                (worktree / "isolated.txt").read_text(encoding="utf-8"),
                "worktree\n",
            )
            self.assertFalse((parent_workspace / "isolated.txt").exists())

    async def test_durable_workspace_file_tools_fail_closed_without_secure_primitives(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            target = workspace / "must-not-exist.txt"
            with patch(
                "opc.layer4_tools.workspace_fs._secure_primitives_available",
                return_value=False,
            ):
                result = await self._call(
                    self._executor(),
                    self._task(workspace),
                    "file_write",
                    {"path": "must-not-exist.txt", "content": "bad"},
                )

            self._assert_workspace_rejection(result)
            self.assertFalse(target.exists())

    async def test_scoped_file_content_tools_reject_cross_boundary_hardlink(self) -> None:
        cases = [
            ("file_read", {"path": "linked.txt"}),
            ("file_write", {"path": "linked.txt", "content": "changed\n"}),
            (
                "file_edit",
                {
                    "path": "linked.txt",
                    "old_string": "outside",
                    "new_string": "changed",
                },
            ),
            (
                "apply_patch",
                {
                    "patch": "\n".join(
                        [
                            "*** Begin Patch",
                            "*** Update File: linked.txt",
                            "@@",
                            "-outside",
                            "+changed",
                            "*** End Patch",
                        ]
                    )
                },
            ),
        ]
        for tool_name, arguments in cases:
            with self.subTest(tool=tool_name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                workspace = root / "workspace"
                sibling = root / "sibling"
                workspace.mkdir()
                sibling.mkdir()
                outside = sibling / "outside.txt"
                outside.write_text("outside\n", encoding="utf-8")
                os.link(outside, workspace / "linked.txt")

                result = await self._call(
                    self._executor(),
                    self._task(workspace),
                    tool_name,
                    arguments,
                )

                self._assert_workspace_rejection(result)
                self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")

    async def test_atomic_updates_do_not_modify_hardlink_created_after_file_check(self) -> None:
        cases = [
            ("file_write", {"path": "target.txt", "content": "changed\n"}),
            (
                "file_edit",
                {
                    "path": "target.txt",
                    "old_string": "inside",
                    "new_string": "changed",
                },
            ),
            (
                "apply_patch",
                {
                    "patch": "\n".join(
                        [
                            "*** Begin Patch",
                            "*** Update File: target.txt",
                            "@@",
                            "-inside",
                            "+changed",
                            "*** End Patch",
                        ]
                    )
                },
            ),
        ]
        original = SecureWorkspace._regular_file
        for tool_name, arguments in cases:
            with self.subTest(tool=tool_name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                workspace = root / "workspace"
                sibling = root / "sibling"
                workspace.mkdir()
                sibling.mkdir()
                target = workspace / "target.txt"
                target.write_text("inside\n", encoding="utf-8")
                outside_link = sibling / "late-link.txt"
                linked = False

                def _link_after_check(fd: int, display_path: Path) -> None:
                    nonlocal linked
                    original(fd, display_path)
                    if not linked:
                        os.link(target, outside_link)
                        linked = True

                with patch.object(
                    SecureWorkspace,
                    "_regular_file",
                    new=staticmethod(_link_after_check),
                ):
                    result = await self._call(
                        self._executor(),
                        self._task(workspace),
                        tool_name,
                        arguments,
                    )

                self.assertTrue(result["success"], result)
                self.assertTrue(result["result"]["success"], result)
                self.assertTrue(linked)
                self.assertEqual(target.read_text(encoding="utf-8"), "changed\n")
                self.assertEqual(
                    outside_link.read_text(encoding="utf-8"),
                    "inside\n",
                )

    async def test_runtime_apply_patch_preflights_every_path_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            workspace.mkdir()
            task = self._task(workspace)
            executor = self._executor()
            result = await self._call(
                executor,
                task,
                "apply_patch",
                {
                    "patch": "\n".join(
                        [
                            "*** Begin Patch",
                            "*** Add File: safe.txt",
                            "+safe",
                            "*** Add File: ../escaped.txt",
                            "+escaped",
                            "*** End Patch",
                        ]
                    )
                },
            )

            self._assert_workspace_rejection(result)
            self.assertFalse((workspace / "safe.txt").exists())
            self.assertFalse((root / "escaped.txt").exists())

    async def test_native_runtime_tool_call_carries_durable_task_workspace(self) -> None:
        class _FileWriteThenFinishLLM:
            def __init__(self, target: Path) -> None:
                self.target = target
                self.calls = 0
                self.messages: list[list[dict[str, Any]]] = []
                self.config = type("Cfg", (), {"max_tokens": 2048})()

            def prepare_user_message_content(
                self,
                content: str,
                attachment_refs: Any = None,
            ) -> str:
                _ = attachment_refs
                return content

            def get_tool_definitions(self, tools: Any) -> Any:
                return tools

            def is_context_overflow_error(self, error: Exception) -> bool:
                _ = error
                return False

            async def chat_stream(self, messages: Any, tools: Any = None):
                _ = tools
                self.calls += 1
                self.messages.append(list(messages))
                yield type(
                    "Evt",
                    (),
                    {"event_type": "message_start", "payload": {}, "model": "stub"},
                )()
                if self.calls == 1:
                    yield type(
                        "Evt",
                        (),
                        {
                            "event_type": "tool_call_delta",
                            "payload": {
                                "index": 0,
                                "id": "write-outside",
                                "name": "file_write",
                                "arguments": (
                                    '{"path": '
                                    f'"{self.target}", '
                                    '"content": "must-not-exist", '
                                    '"task": null}'
                                ),
                            },
                            "model": "stub",
                        },
                    )()
                else:
                    yield type(
                        "Evt",
                        (),
                        {
                            "event_type": "assistant_delta",
                            "payload": {"text": "Handled the rejected tool result."},
                            "model": "stub",
                        },
                    )()
                yield type(
                    "Evt",
                    (),
                    {
                        "event_type": "message_stop",
                        "payload": {"finish_reason": "stop"},
                        "model": "stub",
                    },
                )()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            sibling = root / "sibling"
            workspace.mkdir()
            sibling.mkdir()
            target = sibling / "escaped.txt"
            llm = _FileWriteThenFinishLLM(target)
            registry = ToolRegistry()
            for tool in create_file_tools():
                registry.register(tool)
            task = self._task(workspace)
            runtime = NativeRuntimeV2(
                llm=llm,
                tool_registry=registry,
                max_iterations=3,
            )

            result = await runtime.run(
                system_prompt="Use native file tools.",
                user_message="Write the requested report.",
                task=task,
                allowed_tools=["file_write"],
            )

            self.assertEqual(result.status, TaskStatus.DONE)
            self.assertFalse(target.exists())
            self.assertEqual(llm.calls, 2)
            tool_messages = [
                message
                for message in llm.messages[1]
                if message.get("role") == "tool"
            ]
            self.assertEqual(len(tool_messages), 1)
            self.assertIn("workspace boundary", str(tool_messages[0]["content"]).lower())


if __name__ == "__main__":
    unittest.main()

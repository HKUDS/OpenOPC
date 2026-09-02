from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from opc.layer2_organization import comms
from opc.layer3_agent.company_workspace_fence import (
    CompanyWorkspaceFenceError,
    capture_company_workspace,
)
from opc.layer4_tools.workspace_fs import SecureWorkspace, WorkspaceBoundaryError


def test_secure_workspace_roundtrip_uses_platform_backend() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir) / "工作 空间"
        root.mkdir()
        with SecureWorkspace(str(root), str(root)) as workspace:
            target = workspace.resolve("nested/结果.txt")
            before, created, written = workspace.write_text(
                target,
                "first\n",
                create_dirs=True,
            )
            assert before == ""
            assert created is True
            assert written == len("first\n".encode("utf-8"))
            assert workspace.read_text(target) == "first\n"
            workspace.mutate_text(
                target, lambda value: value.replace("first", "second")
            )
            moved = workspace.resolve("delivered/结果.txt")
            workspace.rename(target, moved)
            assert workspace.read_text(moved) == "second\n"
            entries = list(
                workspace.iter_entries(
                    workspace.resolve(".", use_output_root=False),
                    recursive=True,
                )
            )
            assert any(entry.parts == ("delivered", "结果.txt") for entry in entries)
            workspace.unlink(moved)


def test_company_comms_delivery_uses_platform_backend() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        layout = comms.resolve_layout(temp_dir, "windows-mac", "session-一")
        comms.ensure_layout(layout, ["ceo", "cto"])
        delivered = comms.send_message(
            layout,
            from_role="cto",
            to_role="ceo",
            subject="跨平台交付",
            body="done",
        )
        messages = comms.list_unread(layout, "ceo")
        assert len(messages) == 1
        assert messages[0].path == delivered
        assert comms.read_message(delivered)[1].strip() == "done"


def test_company_meeting_transcript_uses_platform_lock() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        layout = comms.resolve_layout(temp_dir, "windows-mac", "session-lock")
        comms.ensure_layout(layout, ["ceo", "cto"])
        meeting = comms.start_meeting(
            layout,
            meeting_id=None,
            topic="Cross-platform review",
            participants=["ceo", "cto"],
            organizer="ceo",
        )
        entry = comms.append_to_transcript(
            layout,
            meeting_id=meeting.meeting_id,
            author="cto",
            content="validated",
        )
        transcript = comms.read_transcript(layout, meeting.meeting_id)
        assert any(item.entry_id == entry.entry_id for item in transcript)
        assert any(item.content.strip() == "validated" for item in transcript)


def test_windows_path_components_reject_ads_and_device_names() -> None:
    if os.name != "nt":
        pytest.skip("Windows path grammar")
    with tempfile.TemporaryDirectory() as temp_dir:
        with SecureWorkspace(temp_dir, temp_dir) as workspace:
            with pytest.raises(WorkspaceBoundaryError):
                workspace.resolve("report.txt:secret")
            with pytest.raises(WorkspaceBoundaryError):
                workspace.resolve("CON.txt")


def test_workspace_fence_rejects_hardlinks() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        source = root / "source.txt"
        source.write_text("outside alias", encoding="utf-8")
        alias = root / "alias.txt"
        try:
            os.link(source, alias)
        except OSError as exc:
            pytest.skip(f"hardlinks unavailable: {exc}")
        with pytest.raises(CompanyWorkspaceFenceError, match="multiply-linked"):
            capture_company_workspace(root)


def test_workspace_fence_rejects_link_root() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        base = Path(temp_dir)
        workspace = base / "workspace"
        workspace.mkdir()
        alias = base / "workspace-link"
        try:
            alias.symlink_to(workspace, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"directory links unavailable: {exc}")
        with pytest.raises(CompanyWorkspaceFenceError, match="link or reparse point"):
            capture_company_workspace(alias)


def test_windows_workspace_rejects_directory_reparse_point() -> None:
    if os.name != "nt":
        pytest.skip("Windows reparse-point behavior")
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        workspace_root = root / "workspace"
        outside = root / "outside"
        workspace_root.mkdir()
        outside.mkdir()
        link = workspace_root / "junction-or-link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"directory symlinks unavailable: {exc}")
        with SecureWorkspace(str(workspace_root), str(workspace_root)) as workspace:
            target = workspace.resolve("junction-or-link/escape.txt")
            with pytest.raises(WorkspaceBoundaryError):
                workspace.write_text(target, "blocked", create_dirs=False)
        assert not (outside / "escape.txt").exists()

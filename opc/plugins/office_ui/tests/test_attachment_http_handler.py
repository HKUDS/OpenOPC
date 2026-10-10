"""Tests for the /api/attachments HTTP handler in server.py.

Attachments are written by the per-project engine that handled the upload
(`projects/{project_id}/attachments/{id}/{filename}`), while the HTTP handler
is built around the root engine.  The handler must therefore locate files
under any project's attachments dir, not just the root engine's active one.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import aiohttp.web

from opc.core.attachment_store import AttachmentStore
from opc.plugins.office_ui.server import _make_attachment_handler

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def _make_engine(opc_home: Path, project_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        opc_home=opc_home,
        attachment_store=AttachmentStore(opc_home, project_id),
    )


def _write_attachment(opc_home: Path, project_id: str, attachment_id: str, filename: str) -> Path:
    dest_dir = opc_home / "projects" / project_id / "attachments" / attachment_id
    dest_dir.mkdir(parents=True)
    dest = dest_dir / filename
    dest.write_bytes(_PNG_BYTES)
    return dest


def _request(attachment_id: str, filename: str) -> SimpleNamespace:
    return SimpleNamespace(match_info={"attachment_id": attachment_id, "filename": filename})


def _handle(engine: SimpleNamespace, attachment_id: str, filename: str) -> aiohttp.web.StreamResponse:
    handler = _make_attachment_handler(engine)
    return asyncio.run(handler(_request(attachment_id, filename)))


def test_serves_attachment_from_active_project(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path, "default")
    _write_attachment(tmp_path, "default", "aid1234567890abc", "image.png")

    response = _handle(engine, "aid1234567890abc", "image.png")

    assert isinstance(response, aiohttp.web.FileResponse)
    assert response.status == 200


def test_serves_attachment_saved_under_other_project(tmp_path: Path) -> None:
    # Upload happened while project "astron-agent" was active; the root
    # engine's store still points at "default".
    engine = _make_engine(tmp_path, "default")
    _write_attachment(tmp_path, "astron-agent", "bid1234567890abc", "image.png")

    response = _handle(engine, "bid1234567890abc", "image.png")

    assert isinstance(response, aiohttp.web.FileResponse)
    assert response.status == 200


def test_missing_attachment_returns_404(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path, "default")

    response = _handle(engine, "does-not-exist", "image.png")

    assert response.status == 404


def test_rejects_path_traversal_components(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path, "default")
    secret = tmp_path / "projects" / "default" / "secret.txt"
    secret.parent.mkdir(parents=True)
    secret.write_text("top secret", encoding="utf-8")

    for attachment_id, filename in [
        ("..", "secret.txt"),
        ("aid", "../secret.txt"),
        ("aid", "..\\secret.txt"),
    ]:
        response = _handle(engine, attachment_id, filename)
        assert response.status in (403, 404)
        assert not isinstance(response, aiohttp.web.FileResponse)

"""Office-UI Server — aiohttp server that bridges OPC Engine with the React+Phaser frontend.

Initializes OPCEngine, opens ui_state.db for agent/chat persistence,
sets up the event adapter pipeline, and serves static files + WebSocket.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from opc.core.windows_ssl import sanitize_windows_sslkeylogfile

sanitize_windows_sslkeylogfile()

import aiohttp.web
import aiosqlite
from loguru import logger

from opc.core.config import OPCConfig, get_opc_home
from opc.engine import OPCEngine
from opc.plugins.office_ui.agent_store import AgentStore
from opc.plugins.office_ui.chat_store import ChatStore
from opc.plugins.office_ui.event_adapter import EventAdapter
from opc.plugins.office_ui.terminal import server_banner
from opc.plugins.office_ui.terminal import status as terminal_status
from opc.plugins.office_ui.ws_handler import WSHandler


# ── Static file paths ────────────────────────────────────────────────────

# Pre-built frontend lives alongside this file
_STATIC_DIR = Path(__file__).parent / "frontend_dist"
_FRONTEND_NO_STORE_HEADERS = {
    "Cache-Control": "no-store, no-cache, max-age=0, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


def _is_under_path(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _acquire_single_instance_lock(opc_home: Path) -> Any | None:
    """Prevent two office-UI servers from sharing one OPC home.

    Two server processes writing the same ui_state.db contend for the sqlite
    write lock and surface as 'database is locked' failures mid-run. The lock
    is advisory (flock), scoped to this OPC home, and released automatically
    when the process exits — including on crash/SIGKILL, so a stale lock file
    can never block a fresh start.
    """
    try:
        import fcntl
    except ImportError:
        return None  # Non-POSIX platform: no flock available, skip the guard.

    lock_path = opc_home / "office_ui.lock"
    lock_file = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.seek(0)
        holder_pid = lock_file.read().strip() or "unknown"
        lock_file.close()
        raise SystemExit(
            f"Another office-UI server (pid {holder_pid}) is already running against "
            f"{opc_home}. Two instances sharing one ui_state.db cause 'database is "
            "locked' failures that can crash in-flight agent runs. Stop the other "
            "instance first, or point this one at a different OPC home."
        )
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file


# ── Application factory ──────────────────────────────────────────────────

async def create_app(
    config: OPCConfig | None = None,
    project_id: str | None = None,
) -> aiohttp.web.Application:
    """Build and return a fully-wired aiohttp Application."""

    app = aiohttp.web.Application()

    # ── Load config from standard location if not provided ─────────
    if config is None:
        config_dir = get_opc_home() / "config"
        if config_dir.is_dir():
            try:
                config = OPCConfig.load(config_dir)
                logger.info(f"Loaded config from {config_dir}")
            except Exception as e:
                logger.warning(f"Failed to load config from {config_dir}: {e}")

    # ── OPC Engine ────────────────────────────────────────────────────
    engine = OPCEngine(config=config, project_id=project_id)

    # ── UI-state database (agents + chat) ─────────────────────────────
    opc_home = engine.opc_home
    instance_lock = _acquire_single_instance_lock(opc_home)
    db_path = opc_home / "ui_state.db"
    db = await aiosqlite.connect(str(db_path))
    # Wait for a concurrent writer (CLI, tooling) instead of failing after
    # sqlite's 5s default with 'database is locked'.
    await db.execute("PRAGMA busy_timeout=30000")

    agent_store = AgentStore(db)
    await agent_store.initialize()

    chat_store = ChatStore(db)
    await chat_store.initialize()

    event_adapter = EventAdapter()

    # ── Initialize engine (this starts all OPC layers) ────────────────
    await engine.initialize()

    # ── WSHandler ─────────────────────────────────────────────────────
    ws_handler = WSHandler(engine, agent_store, chat_store, event_adapter)

    # Wire engine callbacks through project-bound wrappers so project switches
    # do not retarget in-flight progress/runtime events to the active view.
    ws_handler._wire_engine_callbacks(engine)

    # Wire EventBus → ws_handler.on_opc_event (subscribe to ALL events),
    # preserving the root project context even when the active UI view changes.
    async def _root_engine_event(event: Any) -> None:
        await ws_handler.on_opc_event(
            event,
            runtime_engine=engine,
            project_id=engine.project_id or "default",
        )

    engine.event_bus.subscribe_all(_root_engine_event)

    # ── Restore persisted mode and load matching agents on startup ───
    await ws_handler.restore_persisted_mode()
    startup_preset = ws_handler._resolve_preset_name()
    agents = await agent_store.load_preset(startup_preset, engine.org_engine)
    logger.info(f"Loaded {len(agents)} preset agents (mode={ws_handler._exec_mode}, preset={startup_preset})")

    # ── Ensure activity + secretary channels (session channels are created on demand)
    await chat_store.ensure_activity_channel()
    await chat_store.ensure_secretary_channel()

    # ── Store references for cleanup ──────────────────────────────────
    app["engine"] = engine
    app["db"] = db
    app["ws_handler"] = ws_handler
    app["instance_lock"] = instance_lock

    # ── Routes ────────────────────────────────────────────────────────
    app.router.add_get("/ws", ws_handler.handle_ws)

    # ── Shadow Mode & Temporal Analytics REST API Handlers ────────────
    sh_handlers = _make_shadow_mode_handlers(engine)
    app.router.add_post("/api/auth/login", sh_handlers["login"])
    app.router.add_get("/api/contractor/tasks", sh_handlers["tasks"])
    app.router.add_post("/api/contractor/submit", sh_handlers["submit"])
    app.router.add_get("/api/analytics/temporal_performance", sh_handlers["performance"])
    app.router.add_get("/api/analytics/changelogs", sh_handlers["changelogs"])

    # Attachment download (must be registered before the SPA catch-all)
    app.router.add_get(
        "/api/attachments/{attachment_id}/{filename}",
        _make_attachment_handler(engine),
    )

    # SPA: serve static files, fallback to index.html
    if _STATIC_DIR.is_dir():
        app.router.add_get("/", _serve_index)
        app.router.add_get("/assets/{path:.*}", _serve_asset)
        # Catch-all for SPA client-side routing
        app.router.add_get("/{path:.*}", _serve_spa_fallback)
    else:
        app.router.add_get("/", _serve_no_build)
        logger.warning(f"Frontend not built: {_STATIC_DIR} does not exist")

    # ── Cleanup on shutdown ───────────────────────────────────────────
    app.on_shutdown.append(_on_shutdown)

    return app


# ── Route handlers ────────────────────────────────────────────────────

async def _serve_index(request: aiohttp.web.Request) -> aiohttp.web.FileResponse:
    return aiohttp.web.FileResponse(_STATIC_DIR / "index.html", headers=_FRONTEND_NO_STORE_HEADERS)


async def _serve_asset(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Serve built frontend assets without allowing stale UI protocol bundles."""
    path = request.match_info.get("path", "")
    file_path = (_STATIC_DIR / "assets" / path).resolve()
    assets_dir = (_STATIC_DIR / "assets").resolve()
    if not _is_under_path(file_path, assets_dir):
        return aiohttp.web.Response(status=403, text="Forbidden")
    if not file_path.is_file():
        return aiohttp.web.Response(status=404, text="Not found")
    return aiohttp.web.FileResponse(file_path, headers=_FRONTEND_NO_STORE_HEADERS)


async def _serve_spa_fallback(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Serve static file if exists, otherwise fall back to index.html for SPA routing."""
    path = request.match_info.get("path", "")
    file_path = (_STATIC_DIR / path).resolve()
    if not _is_under_path(file_path, _STATIC_DIR.resolve()):
        return aiohttp.web.Response(status=403, text="Forbidden")
    if file_path.is_file():
        return aiohttp.web.FileResponse(file_path, headers=_FRONTEND_NO_STORE_HEADERS)
    # SPA fallback
    return aiohttp.web.FileResponse(_STATIC_DIR / "index.html", headers=_FRONTEND_NO_STORE_HEADERS)


def _make_shadow_mode_handlers(engine: OPCEngine):
    """Factory creating HTTP handlers for Shadow Mode authentication, contractor tasks, and analytics."""
    from opc.core.auth import create_jwt_token, verify_jwt_token, verify_password
    from opc.layer0_interaction.message_bus import MessageBus
    from opc.layer6_observability.opc_logger import OPCLogger

    async def handle_login(request: aiohttp.web.Request) -> aiohttp.web.Response:
        data = await request.json()
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", "")).strip()
        if not username or not password:
            return aiohttp.web.json_response({"error": "Username and password required"}, status=400)

        employee = await engine.store.get_employee_by_username(username)
        if not employee or not verify_password(password, employee.get("hashed_password", "")):
            return aiohttp.web.json_response({"error": "Invalid credentials or contractor not registered"}, status=401)

        payload = {
            "sub": employee["employee_id"],
            "username": employee["username"],
            "name": employee["name"],
            "role_id": employee["role_id"],
            "access_level": employee.get("access_level", "worker"),
        }
        token = create_jwt_token(payload)
        return aiohttp.web.json_response({"token": token, "user": payload})

    async def handle_contractor_tasks(request: aiohttp.web.Request) -> aiohttp.web.Response:
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.replace("Bearer ", "").strip() if "Bearer " in auth_header else request.query.get("token", "")
        user = verify_jwt_token(token) if token else None
        if not user:
            return aiohttp.web.json_response({"error": "Unauthorized"}, status=401)

        user_role = user.get("role_id", "")
        all_tasks = await engine.store.list_tasks(status=None)
        assigned_tasks = [
            {
                "id": t.id,
                "title": t.title,
                "description": t.description,
                "status": t.status.value if hasattr(t.status, "value") else str(t.status),
                "priority": t.priority,
                "assigned_to": t.assigned_to,
            }
            for t in all_tasks
            if str(t.assigned_to or t.metadata.get("owner_role") or "").strip() == user_role
            and (t.status in ("running", "awaiting_human", "pending") or t.metadata.get("sub_state") == "AWAITING_HUMAN_DELIVERABLE")
        ]
        return aiohttp.web.json_response({"tasks": assigned_tasks, "user": user})

    async def handle_contractor_submit(request: aiohttp.web.Request) -> aiohttp.web.Response:
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.replace("Bearer ", "").strip() if "Bearer " in auth_header else ""
        user = verify_jwt_token(token) if token else None
        if not user:
            return aiohttp.web.json_response({"error": "Unauthorized"}, status=401)

        task_id = ""
        summary_notes = ""
        artifacts_list = []
        file_content = ""

        if request.content_type.startswith("multipart/form-data"):
            reader = await request.multipart()
            while True:
                field = await reader.next()
                if field is None:
                    break
                if field.name == "task_id":
                    task_id = (await field.text()).strip()
                elif field.name == "notes":
                    summary_notes = (await field.text()).strip()
                elif field.name == "file":
                    filename = field.filename
                    if filename:
                        safe_filename = Path(filename).name if filename else ""
                        safe_task_id = re.sub(r'[^a-zA-Z0-9_\-]', '', task_id) or "general"
                        dest_dir = (Path(".opc/workspace/deliverables") / safe_task_id).resolve()
                        base_deliverables_dir = Path(".opc/workspace/deliverables").resolve()
                        base_deliverables_dir.mkdir(parents=True, exist_ok=True)
                        dest_dir.mkdir(parents=True, exist_ok=True)

                        if not _is_under_path(dest_dir, base_deliverables_dir) or not safe_filename:
                            return aiohttp.web.json_response({"error": "Invalid upload path"}, status=400)

                        dest_path = dest_dir / safe_filename
                        content_bytes = await field.read()
                        with open(dest_path, "wb") as f:
                            f.write(content_bytes)

                        try:
                            extracted = dest_path.read_text(encoding="utf-8", errors="ignore")
                            file_content += f"\n\n--- Attached File ({safe_filename}) ---\n{extracted}"
                        except Exception:
                            file_content += f"\n\nUploaded file: {dest_path} ({len(content_bytes)} bytes)"

                        artifacts_list.append({"name": safe_filename, "path": str(dest_path), "size": len(content_bytes)})
        else:
            data = await request.json()
            task_id = str(data.get("task_id", "")).strip()
            summary_notes = str(data.get("notes", "")).strip()

        final_content = (summary_notes + "\n" + file_content).strip()
        if not final_content or not task_id:
            return aiohttp.web.json_response({"error": "Task ID and notes/file content required"}, status=400)

        # Update persistent task status and result in store directly
        task_obj = await engine.store.get_task(task_id)
        if task_obj:
            task_obj.status = TaskStatus.DONE
            task_obj.result = {
                "summary": final_content,
                "artifacts": artifacts_list,
                "submitted_by_human": True,
                "contractor_username": user.get("username", "human_contractor"),
            }
            await engine.store.save_task(task_obj)

        opc_log = OPCLogger(store=engine.store)
        await opc_log.log_trajectory_step(
            task_id=task_id,
            role_id=user.get("role_id", ""),
            employee_id=user.get("sub", "human_contractor"),
            action="HUMAN_DELIVERABLE_SUBMISSION",
            content=final_content,
            artifacts=artifacts_list,
        )

        mbus = MessageBus()
        mbus.publish_event(
            f"deliverable_completed:{task_id}",
            {
                "task_id": task_id,
                "summary": final_content,
                "content": final_content,
                "artifacts": artifacts_list,
                "username": user.get("username", "human_contractor"),
            },
        )
        return aiohttp.web.json_response({"success": True, "task_id": task_id})

    async def handle_temporal_performance(request: aiohttp.web.Request) -> aiohttp.web.Response:
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.replace("Bearer ", "").strip() if "Bearer " in auth_header else ""
        user = verify_jwt_token(token) if token else None
        if not user:
            return aiohttp.web.json_response({"error": "Unauthorized"}, status=401)

        interval = request.query.get("interval", "weekly")
        data = await engine.store.get_temporal_performance(interval=interval)
        return aiohttp.web.json_response(data)

    async def handle_changelogs(request: aiohttp.web.Request) -> aiohttp.web.Response:
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.replace("Bearer ", "").strip() if "Bearer " in auth_header else ""
        user = verify_jwt_token(token) if token else None
        if not user:
            return aiohttp.web.json_response({"error": "Unauthorized"}, status=401)

        limit = int(request.query.get("limit", "100"))
        search = request.query.get("search", None)
        changelogs = await engine.store.get_org_changelogs(limit=limit, search=search)
        return aiohttp.web.json_response({"changelogs": changelogs})

    return {
        "login": handle_login,
        "tasks": handle_contractor_tasks,
        "submit": handle_contractor_submit,
        "performance": handle_temporal_performance,
        "changelogs": handle_changelogs,
    }


def _make_attachment_handler(engine: OPCEngine):
    """Factory that returns an HTTP handler for serving stored attachments."""
    import mimetypes as _mt

    async def _handle(request: aiohttp.web.Request) -> aiohttp.web.Response:
        attachment_id = request.match_info["attachment_id"]
        filename = request.match_info["filename"]
        att_store = getattr(engine, "attachment_store", None)
        if not att_store:
            return aiohttp.web.Response(status=503, text="Attachment store not available")
        file_path = att_store.base_dir / attachment_id / filename
        if not file_path.is_file():
            return aiohttp.web.Response(status=404, text="Not found")
        # Path-traversal guard
        if not _is_under_path(file_path.resolve(), att_store.base_dir.resolve()):
            return aiohttp.web.Response(status=403, text="Forbidden")
        ct, _ = _mt.guess_type(filename)
        headers = {"Cache-Control": "public, max-age=86400"}
        return aiohttp.web.FileResponse(file_path, headers=headers)

    return _handle


async def _serve_no_build(request: aiohttp.web.Request) -> aiohttp.web.Response:
    return aiohttp.web.Response(
        text=(
            "<h1>OpenOPC Office UI</h1>"
            "<p>Frontend not built. Run <code>cd opc/plugins/office_ui/frontend_src && npm install && npm run build</code></p>"
        ),
        content_type="text/html",
    )


async def _on_shutdown(app: aiohttp.web.Application) -> None:
    """Graceful cleanup."""
    ws_handler = app.get("ws_handler")
    if ws_handler:
        await ws_handler.shutdown()
        await ws_handler.flush_all_progress()
    engine = app.get("engine")
    db = app.get("db")
    if engine:
        await engine.shutdown()
    if db:
        await db.close()
    instance_lock = app.get("instance_lock")
    if instance_lock is not None:
        try:
            instance_lock.close()
        except Exception:
            pass
    logger.info("Office-UI server shut down")


# ── Entry point ───────────────────────────────────────────────────────

def run_server(
    host: str = "0.0.0.0",
    port: int = 8765,
    config: OPCConfig | None = None,
    project_id: str | None = None,
) -> None:
    """Create and run the office-UI server (blocking)."""

    async def _start() -> None:
        app = await create_app(config=config, project_id=project_id)
        runner = aiohttp.web.AppRunner(app)
        await runner.setup()
        site = aiohttp.web.TCPSite(runner, host, port)
        await site.start()
        logger.info(f"Office-UI running at http://{host}:{port}")
        server_banner(host=host, port=port, project_id=project_id)
        # Keep running until interrupted
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        finally:
            await runner.cleanup()

    try:
        asyncio.run(_start())
    except KeyboardInterrupt:
        terminal_status("Shutting down Office UI", kind="warning")
    except SystemExit as exc:
        if exc.code and not isinstance(exc.code, int):
            terminal_status(str(exc.code), kind="error")
            raise SystemExit(1) from None
        raise

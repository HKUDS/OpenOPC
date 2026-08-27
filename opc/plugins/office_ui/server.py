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
from opc.core.workspace_trust import WorkspaceTrustRequired
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
            except WorkspaceTrustRequired:
                raise
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

    # LLM Configuration REST API (registered before SPA catch-all)
    app.router.add_get("/api/llm/config", _handle_get_llm_config_api)
    app.router.add_post("/api/llm/config", _handle_update_llm_config_api)

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


async def _handle_get_llm_config_api(request: aiohttp.web.Request) -> aiohttp.web.Response:
    try:
        app = request.app
        opc_home = app.get("opc_home") or get_opc_home()
        config = app.get("config")
        if not config:
            config = OPCConfig.load(opc_home)
        llm_cfg = config.llm
        payload = {
            "ok": True,
            "provider": getattr(llm_cfg, "provider", "") or "",
            "default_model": llm_cfg.default_model,
            "api_base": llm_cfg.api_base,
            "api_key": "***" if llm_cfg.api_key else "",
            "has_api_key": bool(llm_cfg.api_key),
            "is_local": getattr(llm_cfg, "is_local", False),
            "context_window": llm_cfg.context_window,
        }
        return aiohttp.web.json_response(payload)
    except Exception as e:
        return aiohttp.web.json_response({"ok": False, "error": str(e)}, status=500)


async def _handle_update_llm_config_api(request: aiohttp.web.Request) -> aiohttp.web.Response:
    try:
        data = await request.json()
        new_model = str(data.get("default_model", "") or "").strip()
        if not new_model:
            return aiohttp.web.json_response({"ok": False, "error": "Model identifier cannot be empty"}, status=400)

        app = request.app
        opc_home = app.get("opc_home") or get_opc_home()
        config = OPCConfig.load(opc_home)

        config.llm.default_model = new_model
        config.llm.api_base = str(data.get("api_base", "") or "").strip()
        new_api_key = str(data.get("api_key", "") or "").strip()
        if new_api_key and new_api_key != "***":
            config.llm.api_key = new_api_key
        config.llm.provider = str(data.get("provider", "") or "").strip()
        config.llm.is_local = bool(data.get("is_local", False))
        config.llm.context_window = int(data.get("context_window", 0) or 0)

        # Save to disk (.opc/config/llm_config.yaml)
        config.save(opc_home)

        engine = app.get("engine")
        if engine and hasattr(engine, "llm"):
            from opc.llm.provider import LLMProvider
            engine.llm = LLMProvider(config.llm, opc_home=opc_home)

        payload = {
            "ok": True,
            "provider": config.llm.provider,
            "default_model": config.llm.default_model,
            "api_base": config.llm.api_base,
            "has_api_key": bool(config.llm.api_key),
            "is_local": config.llm.is_local,
            "context_window": config.llm.context_window,
        }
        return aiohttp.web.json_response(payload)
    except Exception as e:
        return aiohttp.web.json_response({"ok": False, "error": str(e)}, status=500)


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

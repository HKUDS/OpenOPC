"""Office-UI Plugin — visual frontend for the OPC multi-agent system.

Registers the `opc ui` CLI command when aiohttp is available.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from opc.core.windows_ssl import (
    format_windows_sslkeylog_warning,
    pop_windows_sslkeylogfile,
)
from opc.plugins.office_ui.terminal import error as terminal_error
from opc.plugins.office_ui.terminal import status as terminal_status

if TYPE_CHECKING:
    import typer

_PLUGIN_DIR = Path(__file__).parent
_FRONTEND_SRC = _PLUGIN_DIR / "frontend_src"
_FRONTEND_DIST = _PLUGIN_DIR / "frontend_dist"


def _latest_mtime(root: Path, *, exclude_dirs: set[str] | None = None) -> float:
    exclude = set(exclude_dirs or set())
    latest = 0.0
    if not root.is_dir():
        return latest
    for path in root.rglob("*"):
        if any(part in exclude for part in path.parts):
            continue
        if not path.is_file():
            continue
        try:
            latest = max(latest, path.stat().st_mtime)
        except OSError:
            continue
    return latest


def _frontend_needs_rebuild() -> bool:
    if not _FRONTEND_DIST.is_dir() or not any(_FRONTEND_DIST.iterdir()):
        return True
    latest_src = _latest_mtime(_FRONTEND_SRC, exclude_dirs={"node_modules"})
    latest_dist = _latest_mtime(_FRONTEND_DIST)
    return latest_src > latest_dist


def _sanitize_windows_ssl_env() -> None:
    """Avoid Windows OpenSSL crashes triggered by SSLKEYLOGFILE."""
    keylog_path = pop_windows_sslkeylogfile()
    if keylog_path:
        terminal_status(
            "SSL keylog environment removed",
            kind="warning",
            detail=format_windows_sslkeylog_warning("opc ui", keylog_path),
        )


def _pip_install(*packages: str) -> None:
    """Install packages using uv pip (if in uv env) or fallback to pip."""
    uv = shutil.which("uv")
    # Detect uv-managed venv: UV_VIRTUAL_ENV set or uv binary available with active venv
    in_uv = bool(uv) and ("UV_VIRTUAL_ENV" in os.environ or "uv" in (sys.prefix or ""))
    if in_uv:
        cmd = [uv, "pip", "install", *packages]
    else:
        cmd = [sys.executable, "-m", "pip", "install", *packages]
    subprocess.check_call(cmd, stdout=subprocess.DEVNULL)


def _ensure_aiohttp() -> None:
    """Install aiohttp + aiosqlite if missing."""
    missing = []
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        missing.append("aiohttp>=3.9.0")
    try:
        import aiosqlite  # noqa: F401
    except ImportError:
        missing.append("aiosqlite>=0.19.0")
    if missing:
        terminal_status("Installing Python dependencies", detail=", ".join(missing))
        _pip_install(*missing)


def _run_npm(npm: str, *args: str) -> None:
    """Run npm in a way that also works when Windows resolves npm.cmd."""
    cmd = [npm, *args]
    if sys.platform == "win32":
        npm_path = Path(npm)
        npm_cmd = npm_path.with_suffix(".cmd")
        if npm_path.suffix.lower() in {".cmd", ".bat"}:
            cmd = [os.environ.get("COMSPEC", "cmd.exe"), "/c", npm, *args]
        elif npm_cmd.exists():
            cmd = [os.environ.get("COMSPEC", "cmd.exe"), "/c", str(npm_cmd), *args]
    subprocess.check_call(cmd, cwd=_FRONTEND_SRC)


def _ensure_frontend() -> None:
    """Build frontend if frontend_dist is missing, empty, or stale."""
    if not _frontend_needs_rebuild():
        return

    if not _FRONTEND_SRC.is_dir():
        terminal_error("Frontend source not found.", detail=str(_FRONTEND_SRC))
        raise SystemExit(1)

    npm = shutil.which("npm")
    if not npm:
        terminal_error("npm not found.", detail="Install Node.js first: https://nodejs.org/")
        raise SystemExit(1)

    if _FRONTEND_DIST.is_dir() and any(_FRONTEND_DIST.iterdir()):
        terminal_status("Frontend source changed", detail="rebuilding Office UI bundle")
    else:
        terminal_status("Building Office UI frontend", detail="first run")

    # npm install
    node_modules = _FRONTEND_SRC / "node_modules"
    if not node_modules.is_dir():
        terminal_status("Installing npm dependencies")
        _run_npm(npm, "install")

    # npm run build → outputs to ../frontend_dist via vite config
    terminal_status("Building frontend bundle")
    _run_npm(npm, "run", "build")

    if _FRONTEND_DIST.is_dir() and any(_FRONTEND_DIST.iterdir()):
        terminal_status("Frontend built successfully", kind="success")
    else:
        terminal_error("Build completed but frontend_dist is empty.")
        raise SystemExit(1)


def register_cli(parent_app: typer.Typer) -> None:
    """Register the `opc ui` command on the parent Typer app."""
    import typer as _typer

    @parent_app.command()
    def ui(
        port: int = _typer.Option(8765, "--port", "-p", help="Server port"),
        host: str = _typer.Option("0.0.0.0", "--host", help="Bind address"),
        project: Optional[str] = _typer.Option(None, "--project", help="Project ID"),
        rebuild: bool = _typer.Option(False, "--rebuild", help="Force rebuild frontend"),
    ) -> None:
        """Launch the Office UI — visual frontend for OPC agents."""
        _sanitize_windows_ssl_env()
        # Auto-install aiohttp if needed
        _ensure_aiohttp()

        # Auto-build frontend if needed (or forced)
        if rebuild and _FRONTEND_DIST.is_dir():
            shutil.rmtree(_FRONTEND_DIST)
        _ensure_frontend()

        from opc.core.config import OPCConfig, get_opc_home

        config_dir = get_opc_home() / "config"
        config = OPCConfig.load(config_dir) if config_dir.exists() else OPCConfig()

        from opc.plugins.office_ui.server import run_server

        run_server(host=host, port=port, config=config, project_id=project)

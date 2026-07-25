"""OpenOPC Shadow Adapter Plugin — Human-in-the-Loop (HITL) Execution Surface.

Auto-registers `ShadowModeAdapter` into OpenOPC's `ADAPTER_CLASSES` registry
upon import and exposes plugin CLI commands for `opc shadow-serve`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

# Import core shadow adapter components
from shadow_adapter.adapter import ShadowModeAdapter
from shadow_adapter.config import ShadowConfig
from shadow_adapter.models import ShadowTask, ShadowTaskStatus
from shadow_adapter.security import SecurityManager
from shadow_adapter.shadow_store import ShadowStore

if TYPE_CHECKING:
    import typer

# Automatically register ShadowModeAdapter with OpenOPC's adapter registry
try:
    from opc.layer3_agent.adapters.registry import ADAPTER_CLASSES

    ADAPTER_CLASSES["shadow"] = ShadowModeAdapter
except ImportError:
    pass


def register_cli_commands(app: typer.Typer) -> None:
    """Register shadow CLI commands under `opc shadow-serve`."""

    @app.command("shadow-serve")
    def shadow_serve_command(
        port: int = 8800,
        host: str = "0.0.0.0",
        db: str = "./shadow_tasks.db",
    ) -> None:
        """Launch the OpenOPC Shadow Adapter API server & Human Portal."""
        import uvicorn
        from shadow_adapter.api.app import create_app

        config = ShadowConfig(api_port=port, api_host=host, db_path=db)
        server_app = create_app(config)
        uvicorn.run(server_app, host=host, port=port)


__all__ = [
    "ShadowModeAdapter",
    "ShadowConfig",
    "ShadowStore",
    "SecurityManager",
    "ShadowTask",
    "ShadowTaskStatus",
    "register_cli_commands",
]

"""Shared LLM Configuration Service for Office UI (REST API and WebSocket)."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from loguru import logger

from opc.core.config import OPCConfig, get_opc_home, resolve_config_dir

_CONFIG_LOCK = threading.Lock()


def get_llm_config_service(engine_or_home: Any = None) -> dict[str, Any]:
    """Retrieve active LLM configuration for Office UI settings modal."""
    opc_home = getattr(engine_or_home, "opc_home", None)
    if opc_home is None:
        opc_home = engine_or_home if isinstance(engine_or_home, Path) else get_opc_home()

    config_dir = resolve_config_dir(opc_home)
    config = getattr(engine_or_home, "config", None)
    if not config or not isinstance(config, OPCConfig):
        config = OPCConfig.load(config_dir)

    llm_cfg = config.llm
    return {
        "ok": True,
        "provider": getattr(llm_cfg, "provider", "") or "",
        "default_model": llm_cfg.default_model,
        "api_base": llm_cfg.api_base,
        "api_key": "***" if llm_cfg.api_key else "",
        "has_api_key": bool(llm_cfg.api_key),
        "is_local": getattr(llm_cfg, "is_local", False),
        "context_window": llm_cfg.context_window,
    }


def update_llm_config_service(engine_or_home: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate, persist, and hot-apply LLM configuration updates atomically."""
    if not isinstance(payload, dict):
        raise ValueError("Invalid payload format; expected a dictionary")

    new_model = str(payload.get("default_model", "") or "").strip()
    if not new_model:
        raise ValueError("Model identifier cannot be empty")

    new_api_base = str(payload.get("api_base", "") or "").strip()
    if new_api_base:
        parsed = urlparse(new_api_base)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"Invalid API base URL scheme '{parsed.scheme}'; expected http or https")

    new_api_key = str(payload.get("api_key", "") or "").strip()
    new_provider = str(payload.get("provider", "") or "").strip()
    is_local = bool(payload.get("is_local", False))

    raw_context_window = payload.get("context_window", 0)
    try:
        context_window = int(raw_context_window or 0)
    except (TypeError, ValueError):
        raise ValueError("Context window must be an integer")
    if context_window < 0 or context_window > 2_000_000:
        raise ValueError("Context window must be between 0 and 2,000,000")

    opc_home = getattr(engine_or_home, "opc_home", None)
    if opc_home is None:
        opc_home = engine_or_home if isinstance(engine_or_home, Path) else get_opc_home()

    config_dir = resolve_config_dir(opc_home)

    with _CONFIG_LOCK:
        config = OPCConfig.load(config_dir)

        config.llm.default_model = new_model
        config.llm.api_base = new_api_base
        config.llm.provider = new_provider
        config.llm.is_local = is_local
        config.llm.context_window = context_window

        # Explicit Key Semantics:
        # - "***": keep existing key
        # - "": explicitly clear key
        # - new string: update key
        if new_api_key == "":
            config.llm.api_key = ""
        elif new_api_key != "***":
            config.llm.api_key = new_api_key

        # Atomically save ONLY llm_config.yaml into canonical config_dir
        saved_path = config.save_llm_config(config_dir)
        logger.info(f"LLM configuration updated and saved to {saved_path}")

        # Hot-apply reconfiguration across all engine consumers
        if hasattr(engine_or_home, "reconfigure_llm"):
            engine_or_home.reconfigure_llm(config.llm)
        elif hasattr(engine_or_home, "config"):
            engine_or_home.config.llm = config.llm
            if hasattr(engine_or_home, "llm") and hasattr(engine_or_home.llm, "config"):
                engine_or_home.llm.config = config.llm

    return {
        "ok": True,
        "provider": config.llm.provider,
        "default_model": config.llm.default_model,
        "api_base": config.llm.api_base,
        "has_api_key": bool(config.llm.api_key),
        "is_local": config.llm.is_local,
        "context_window": config.llm.context_window,
    }

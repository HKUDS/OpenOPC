"""Canonical execution-agent identities shared by every OpenOPC surface.

Agent names cross the CLI, Office UI, recruitment, task dispatch, company
topology, checkpoint, and adapter boundaries.  Keeping separate allowlists at
those boundaries can make a valid UI choice silently fall back to ``native``.
This module is the dependency-light source of truth for those identities.
"""

from __future__ import annotations

from typing import Any


NATIVE_EXECUTION_AGENT = "native"
EXTERNAL_EXECUTION_AGENTS: frozenset[str] = frozenset({
    "claude_code",
    "cursor",
    "codex",
    "opencode",
    "jiuwen",
    "jiuwenswarm",
})
EXECUTION_AGENTS: frozenset[str] = frozenset({
    NATIVE_EXECUTION_AGENT,
    *EXTERNAL_EXECUTION_AGENTS,
})
EXECUTION_AGENT_LABELS: dict[str, str] = {
    "native": "OpenOPC Native",
    "codex": "Codex",
    "claude_code": "Claude Code",
    "cursor": "Cursor",
    "opencode": "OpenCode",
    "jiuwen": "JiuwenSwarm-single",
    "jiuwenswarm": "JiuwenSwarm-team",
}


def normalize_execution_agent(value: Any, default: str | None = None) -> str | None:
    """Return a canonical execution-agent name, or a valid fallback."""

    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized in EXECUTION_AGENTS:
        return normalized
    fallback = str(default or "").strip().lower().replace("-", "_")
    if fallback in EXECUTION_AGENTS:
        return fallback
    return None


def execution_agent_label(value: Any) -> str:
    """Return the stable user-facing label without changing the wire identity."""

    raw = str(value or "").strip()
    normalized = normalize_execution_agent(raw)
    return EXECUTION_AGENT_LABELS.get(normalized or "", raw)

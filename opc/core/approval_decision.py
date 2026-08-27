"""Canonical tokens shared by approval producers and durable ledgers."""

from __future__ import annotations

from typing import Any


_ESCALATION_DECISION_TOKENS: frozenset[str] = frozenset({
    "approve_once",
    "approve_session",
    "always_project",
    "always_global",
    "deny",
})

_ESCALATION_APPROVE_SYNONYMS: frozenset[str] = frozenset({
    "approve",
    "approved",
    "yes",
    "y",
    "ok",
    "allow",
    "同意",
    "批准",
    "允许",
})

_ESCALATION_DENY_SYNONYMS: frozenset[str] = frozenset({
    "no",
    "n",
    "denied",
    "reject",
    "rejected",
    "拒绝",
    "不允许",
})


def normalize_escalation_reply(reply: str) -> str:
    """Map an approval reply to its canonical token, or ``""`` if unknown."""

    text = str(reply or "").strip().lower()
    if text in _ESCALATION_DECISION_TOKENS:
        return text
    if text in _ESCALATION_APPROVE_SYNONYMS:
        return "approve_once"
    if text in _ESCALATION_DENY_SYNONYMS:
        return "deny"
    return ""


def canonical_tool_permission_decision(value: Any) -> str:
    """Extract a canonical ToolCall approval token from a durable decision."""

    if isinstance(value, dict):
        raw = (
            value.get("option_id")
            or value.get("checkpoint_reply_kind")
            or value.get("text")
            or ""
        )
    else:
        raw = value
    return normalize_escalation_reply(str(raw or ""))

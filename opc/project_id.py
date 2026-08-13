"""Canonical project-ID contract shared by OpenOPC entry points."""

from __future__ import annotations

import re
from typing import Final


PROJECT_ID_POLICY_VERSION: Final = 1
PROJECT_ID_PATTERN: Final = r"[A-Za-z0-9][A-Za-z0-9_-]*"
_PROJECT_ID_RE: Final = re.compile(PROJECT_ID_PATTERN)


def is_valid_project_id(value: object) -> bool:
    """Return whether *value* is exactly one valid project ID."""

    return isinstance(value, str) and _PROJECT_ID_RE.fullmatch(value) is not None


def project_id_policy() -> dict[str, object]:
    """Return the versioned, ECMAScript-compatible UI validation policy."""

    return {
        "version": PROJECT_ID_POLICY_VERSION,
        "pattern": PROJECT_ID_PATTERN,
    }

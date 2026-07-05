"""The Physical AI hub map: link integrity + readiness self-assessment completeness.

docs/physical-ai.md is the single entry point for the pack (Staff · Operate ·
Measure). This guards it against rot — every relative link must resolve, and the
readiness scorecard must carry all 8 checks and the L0→L4 maturity ladder.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HUB = REPO_ROOT / "docs" / "physical-ai.md"

_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def test_hub_exists_and_relative_links_resolve() -> None:
    assert HUB.exists()
    text = HUB.read_text(encoding="utf-8")
    targets = [m.group(1).split("#", 1)[0] for m in _LINK.finditer(text)]
    relative = [t for t in targets if t and not t.startswith(("http://", "https://"))]
    assert relative, "hub should link to sibling artifacts"
    missing = [t for t in relative if not (HUB.parent / t).resolve().exists()]
    assert not missing, f"hub has broken relative links: {missing}"


def test_readiness_self_assessment_is_complete() -> None:
    text = HUB.read_text(encoding="utf-8")
    checks = [
        "Episode capture",
        "Independent referee",
        "Safety as a terminal gate",
        "Failure taxonomy",
        "Regression assets",
        "capability memory",
        "Release ladder",
        "Human-burden ratio",
    ]
    for c in checks:
        assert c in text, f"readiness scorecard missing check: {c}"
    for level in ("L0", "L1", "L2", "L3", "L4"):
        assert level in text, f"maturity ladder missing {level}"
    # The honest-scoring rule and the safety prerequisite must be stated.
    assert "no evidence" in text.lower()


def test_readiness_infographic_present() -> None:
    ig = REPO_ROOT / "landing" / "infographics" / "physical-ai-readiness.html"
    assert ig.exists()
    body = ig.read_text(encoding="utf-8")
    assert "no evidence" in body.lower()
    assert "Industrial learning system" in body

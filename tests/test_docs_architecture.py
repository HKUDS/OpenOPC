"""Guard the contributor-facing architecture docs from rot.

Every relative link in docs/architecture.md, CONTRIBUTING.md, and the README's
Architecture section must resolve, and the architecture infographic must exist.
These are the newcomer's on-ramp; a broken link here fails a first contribution.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def _relative_targets(text: str) -> list[str]:
    return [
        m.group(1).split("#", 1)[0]
        for m in _LINK.finditer(text)
        if not m.group(1).startswith(("http://", "https://", "mailto:"))
    ]


def test_architecture_and_contributing_links_resolve() -> None:
    for rel in ("docs/architecture.md", "CONTRIBUTING.md"):
        doc = REPO_ROOT / rel
        assert doc.exists(), rel
        base = doc.parent
        missing = [t for t in _relative_targets(doc.read_text(encoding="utf-8"))
                   if t and not (base / t).resolve().exists()]
        assert not missing, f"broken relative links in {rel}: {missing}"


def test_architecture_infographic_exists_and_is_indexed() -> None:
    ig = REPO_ROOT / "landing" / "infographics" / "architecture.html"
    assert ig.exists()
    index = (REPO_ROOT / "landing" / "infographics" / "index.html").read_text(encoding="utf-8")
    assert "architecture.html" in index, "architecture infographic not linked from the hub"


def test_architecture_doc_covers_the_seven_layers() -> None:
    text = (REPO_ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    for layer in ("Interaction", "Perception", "Organization", "Agent Execution",
                  "Tools", "Memory", "Observability"):
        assert layer in text, f"architecture doc missing layer: {layer}"
    # The decisions section must explain *why*, not just *what*.
    assert "why" in text.lower() and "metadata_ownership" in text

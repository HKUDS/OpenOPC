"""Employee-loop-incentive pack: hub link-integrity, scorecard, skill mount, and
the reference smart contract's three encoded invariants.

The doctrine (skills/core/employee_loop_incentive.md) is mounted on the CNO-equivalent
role and its payout math is sketched in contracts/VerifiedClosePayout.sol. These tests
keep the map from rotting and assert the contract sketch still encodes maker != checker,
compounding vesting, and a safety-zero gate.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HUB = REPO_ROOT / "docs" / "employee-loop-incentive.md"
SKILL = REPO_ROOT / "skills" / "core" / "employee_loop_incentive.md"
CONTRACT = REPO_ROOT / "contracts" / "VerifiedClosePayout.sol"

_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def test_hub_relative_links_resolve() -> None:
    assert HUB.exists()
    text = HUB.read_text(encoding="utf-8")
    rel = [
        m.group(1).split("#", 1)[0]
        for m in _LINK.finditer(text)
        if not m.group(1).startswith(("http://", "https://"))
    ]
    missing = [t for t in rel if t and not (HUB.parent / t).resolve().exists()]
    assert not missing, f"broken relative links in hub: {missing}"


def test_incentive_scorecard_complete() -> None:
    text = HUB.read_text(encoding="utf-8")
    for check in (
        "Visible gap board",
        "Cheap, bounded experiments",
        "Independent referee",
        "Safety is a hard zero",
        "Pay the verified close",
        "Compounding payout",
        "immutable settlement",
        "Public compounding ledger",
    ):
        assert check in text, f"scorecard missing check: {check}"
    for level in ("L0", "L1", "L2", "L3", "L4"):
        assert level in text, f"maturity ladder missing {level}"
    assert "no evidence" in text.lower()


def test_skill_present_and_mounted_on_the_cno_role_only() -> None:
    body = SKILL.read_text(encoding="utf-8").lower()
    for anchor in ("maker ≠ checker", "compounding", "safety is a hard zero", "verified close"):
        assert anchor in body, anchor

    from opc.market.architecture_registry import get_preset

    preset = get_preset("physical-ai-robotics-company")
    roles = {r["id"]: (r.get("skill_refs") or []) for r in preset.roles}
    assert "employee_loop_incentive" in roles["founding_ai_native_lead"]
    # It's a leadership/culture skill — must not leak onto every engineer.
    leaked = [rid for rid, refs in roles.items()
              if rid != "founding_ai_native_lead" and "employee_loop_incentive" in refs]
    assert not leaked, f"CNO skill leaked to {leaked}"


def test_contract_sketch_encodes_the_three_invariants() -> None:
    src = CONTRACT.read_text(encoding="utf-8")
    # It must be unmistakably a reference, not production.
    assert "REFERENCE SKETCH" in src and "unaudited" in src.lower()
    # 1. maker != checker — enforced in code, at attestation.
    assert 'require(msg.sender != c.maker' in src
    # 2. compounding vesting — a stream that re-verification extends.
    assert "vestingEnd" in src and "reverifyDurable" in src
    # 3. safety-zero — a terminal gate that zeroes the unreleased reward.
    assert "flagSafety" in src and "SafetyZeroed" in src
    # Reward triggers on a verified close, never a proposal.
    assert "Status.Verified" in src

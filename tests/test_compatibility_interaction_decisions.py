from __future__ import annotations

from typing import Any

import pytest

from opc.core.models import ExecutionCheckpoint
from opc.engine import OPCEngine
from opc.layer0_interaction.coordinator import InteractionCoordinator


@pytest.mark.parametrize(
    (
        "checkpoint_type",
        "payload",
        "reply",
        "expected_fields",
        "expected_validation",
    ),
    [
        (
            "company_staffing_selection",
            {
                "staffing_roles": [
                    {
                        "role_id": "researcher",
                        "default_selection": {
                            "kind": "template",
                            "id": "investment-analyst",
                        },
                        "default_agent": "native",
                    },
                    {
                        "role_id": "reviewer",
                        "default_selection": {"kind": "fallback", "id": ""},
                        "selected_agent": "native",
                    },
                ]
            },
            "approve",
            {
                "staffing_action": "manual_approve",
                "staffing_selections": {
                    "researcher": {
                        "kind": "template",
                        "id": "investment-analyst",
                    },
                    "reviewer": {"kind": "fallback", "id": ""},
                },
                "recruitment_role_agents": {
                    "researcher": "native",
                    "reviewer": "native",
                },
            },
            "",
        ),
        (
            "company_run_failure_review",
            {},
            "approve",
            {"checkpoint_reply_kind": "acknowledge"},
            "",
        ),
        (
            "company_run_failure_review",
            {},
            "deny",
            {"checkpoint_reply_kind": "dismiss"},
            "",
        ),
        (
            "company_delivery_feedback",
            {},
            "deny",
            {"checkpoint_reply_kind": "ignore"},
            "",
        ),
        (
            "task_user_input",
            {},
            "",
            {},
            "empty_decision",
        ),
    ],
)
def test_compatibility_builder_emits_only_coordinator_valid_typed_defaults(
    checkpoint_type: str,
    payload: dict[str, Any],
    reply: str,
    expected_fields: dict[str, Any],
    expected_validation: str,
) -> None:
    checkpoint = ExecutionCheckpoint(
        checkpoint_id=f"compat-{checkpoint_type}",
        project_id="project-a",
        checkpoint_type=checkpoint_type,
        payload={
            **payload,
            "interaction": {"kind": checkpoint_type},
        },
    )

    decision = OPCEngine.build_compatibility_checkpoint_decision(
        checkpoint,
        reply,
        None,
    )

    for key, value in expected_fields.items():
        assert decision.get(key) == value
    assert (
        InteractionCoordinator.validate_decision(checkpoint, decision)
        == expected_validation
    )
    if checkpoint_type == "company_delivery_feedback":
        assert "human_feedback_text" not in decision

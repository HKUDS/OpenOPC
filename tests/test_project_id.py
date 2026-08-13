from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

from opc.plugins.office_ui.services.project import ProjectService
from opc.project_id import is_valid_project_id, project_id_policy


VALID_PROJECT_IDS = ("a", "Z", "0", "project-1", "project_name", "0-x_y")
INVALID_PROJECT_IDS = ("", "-abc", "_abc", "---", "___", "with space", "知识工程", "abc\n")


def test_project_id_validator_uses_full_match() -> None:
    for project_id in VALID_PROJECT_IDS:
        assert is_valid_project_id(project_id)
    for project_id in INVALID_PROJECT_IDS:
        assert not is_valid_project_id(project_id)
    assert not is_valid_project_id(None)


def test_advertised_policy_matches_authoritative_validator() -> None:
    policy = project_id_policy()
    assert policy["version"] == 1
    expression = re.compile(str(policy["pattern"]))
    for project_id in (*VALID_PROJECT_IDS, *INVALID_PROJECT_IDS):
        assert (expression.fullmatch(project_id) is not None) is is_valid_project_id(project_id)


def test_project_list_exposes_versioned_project_id_policy() -> None:
    context = SimpleNamespace(
        active_engine_project_id=lambda: "default",
        list_project_entries=lambda: [{"id": "default", "name": "default"}],
        normalize_project_id=lambda value: str(value or "default"),
    )
    result = asyncio.run(ProjectService(context).list())

    assert result.payload["project_id_policy"] == project_id_policy()

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from opc.core.config import OPCConfig, RoleConfig


def _make_role() -> RoleConfig:
    return RoleConfig(
        id="student",
        name="Student",
        responsibility="Learn.",
        reports_to="owner",
        tools=["file_read"],
    )


def _make_context(cfg: OPCConfig, *, exec_mode: str, company_profile: str):
    from opc.plugins.office_ui.services.context import ModeState, OfficeServiceContext

    engine = SimpleNamespace(config=cfg, org_engine=MagicMock())
    context = OfficeServiceContext(
        engine=engine,
        agent_store=None,
        chat_store=MagicMock(),
        event_adapter=MagicMock(),
        mode_state=ModeState(exec_mode=exec_mode, company_profile=company_profile),
    )
    return engine, context


class UpdateRolePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_role_persists_tools(self) -> None:
        """Role tool edits persist when the active org is an editable custom org.

        Role mutation now lives in OrgService (the WS handler delegates to it),
        and writes are intentionally refused for built-in read-only orgs, so
        persistence must be asserted against an editable custom org.
        """
        from opc.plugins.office_ui.services.org import OrgService

        cfg = OPCConfig()
        cfg.org.company_profile = "custom"
        cfg.org.organization_id = "lab_org"
        cfg.org.organization_name = "Lab Org"
        cfg.org.roles = [_make_role()]
        engine, context = _make_context(cfg, exec_mode="custom", company_profile="custom")

        with patch.object(OPCConfig, "save", autospec=True) as save:
            result = await OrgService(context).update_role(
                "student",
                {"tools": ["file_read", " ", "web_search", ""]},
            )

        self.assertEqual(cfg.org.roles[0].tools, ["file_read", "web_search"])
        save.assert_called_once_with(cfg)
        engine.org_engine.reload_from_config.assert_called_once()
        self.assertEqual(result.payload["action"], "role_updated")
        self.assertEqual(result.payload["role_id"], "student")
        self.assertEqual(result.payload["role"]["tools"], ["file_read", "web_search"])

    async def test_update_role_tools_rejected_for_readonly_builtin_org(self) -> None:
        """Built-in (corporate) orgs are read-only: tool edits must not persist."""
        from opc.plugins.office_ui.services.models import ServiceError
        from opc.plugins.office_ui.services.org import OrgService

        cfg = OPCConfig()
        cfg.org.roles = [_make_role()]
        engine, context = _make_context(cfg, exec_mode="company", company_profile="corporate")

        with patch.object(OPCConfig, "save", autospec=True) as save:
            with self.assertRaises(ServiceError) as raised:
                await OrgService(context).update_role(
                    "student",
                    {"tools": ["file_read", "web_search"]},
                )

        self.assertEqual(raised.exception.code, "org_read_only")
        self.assertEqual(cfg.org.roles[0].tools, ["file_read"])
        save.assert_not_called()
        engine.org_engine.reload_from_config.assert_not_called()

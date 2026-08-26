from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from opc.core.config import ExternalTeamBindingConfig, OPCConfig, RoleConfig


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
    async def test_deleting_team_boundary_removes_binding_atomically(self) -> None:
        from opc.layer2_organization.org_engine import OrgEngine
        from opc.plugins.office_ui.services.context import ModeState, OfficeServiceContext
        from opc.plugins.office_ui.services.org import OrgService

        cfg = OPCConfig()
        cfg.org.company_profile = "custom"
        cfg.org.organization_id = "team_org"
        cfg.org.roles = [
            RoleConfig(id="cto", name="CTO", responsibility="Lead tech.", reports_to="owner"),
            RoleConfig(id="engineer", name="Engineer", responsibility="Build.", reports_to="cto"),
        ]
        cfg.org.external_team_bindings = [
            ExternalTeamBindingConfig(boundary_role_id="cto")
        ]
        agent_store = SimpleNamespace(
            get_all=AsyncMock(return_value=[]),
            remove_agent=AsyncMock(),
            sync_custom_shadow=AsyncMock(),
        )
        engine = SimpleNamespace(config=cfg, org_engine=OrgEngine(cfg))
        context = OfficeServiceContext(
            engine=engine,
            agent_store=agent_store,
            chat_store=MagicMock(),
            event_adapter=MagicMock(),
            mode_state=ModeState(exec_mode="custom", company_profile="custom"),
        )
        context.persist_runtime_config = lambda: None

        result = await OrgService(context).delete_role("cto")

        self.assertEqual(cfg.org.external_team_bindings, [])
        self.assertEqual(cfg.org.roles[0].reports_to, "owner")
        self.assertEqual(result.payload["removed_external_team_bindings"], ["cto"])

    async def test_corporate_external_team_binding_is_runtime_editable(self) -> None:
        """Corporate structure stays read-only while execution bindings remain configurable."""
        from opc.layer2_organization.org_engine import OrgEngine
        from opc.plugins.office_ui.services.context import ModeState, OfficeServiceContext
        from opc.plugins.office_ui.services.org import OrgService
        from opc.plugins.office_ui.ws_handler import WSHandler

        cfg = OPCConfig()
        org = OrgEngine(cfg)
        engine = SimpleNamespace(
            config=cfg,
            org_engine=org,
            store=None,
            adapter_registry=SimpleNamespace(describe_all=lambda: []),
            project_id="default",
        )
        context = OfficeServiceContext(
            engine=engine,
            agent_store=None,
            chat_store=MagicMock(),
            event_adapter=MagicMock(),
            mode_state=ModeState(exec_mode="company", company_profile="corporate"),
        )
        context.persist_runtime_config = lambda: None
        service = OrgService(context)

        result = await service.bind_external_team({
            "boundary_role_id": "cto",
            "external_agent": "jiuwenswarm",
            "scope": "subtree",
            "collapse_subtree": True,
            "metadata": {"deliverables": ["source_code"]},
        })

        self.assertEqual(result.payload["action"], "external_team_bound")
        self.assertEqual(cfg.org.external_team_bindings[0].boundary_role_id, "cto")
        manifest = result.payload["binding"]["capability_manifest"]
        self.assertEqual(manifest["organizational_identity"], "cto")
        self.assertEqual(manifest["deliverables"], ["source_code"])
        info = await service.info()
        self.assertEqual(info.payload["external_execution_units"][0]["boundary_role_id"], "cto")
        role_by_id = {role["role_id"]: role for role in info.payload["roles"]}
        self.assertTrue(role_by_id["cto"]["external_team_boundary"])
        self.assertTrue(role_by_id["senior_engineer"]["staffing_locked"])
        self.assertIn("bind_external_team", WSHandler._HANDLERS)
        self.assertIn("unbind_external_team", WSHandler._HANDLERS)

        await service.unbind_external_team("cto")
        self.assertEqual(cfg.org.external_team_bindings, [])

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

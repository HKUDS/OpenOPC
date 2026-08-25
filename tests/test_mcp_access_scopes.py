from __future__ import annotations

import unittest
from types import SimpleNamespace

from opc.core.config import MCPServerConfig, OPCConfig
from opc.core.models import Task
from opc.engine import OPCEngine
from opc.layer4_tools.registry import ToolDefinition, ToolRegistry


async def _ok_tool() -> dict[str, bool]:
    return {"ok": True}


class _FakeMCPManager:
    def __init__(self) -> None:
        self.connect_calls: list[dict[str, object]] = []
        self.register_calls: list[dict[str, object]] = []

    async def connect_local(self, **kwargs: object) -> SimpleNamespace:
        self.connect_calls.append(kwargs)
        return SimpleNamespace(name=str(kwargs["name"]))

    async def register_tools(
        self,
        conn: SimpleNamespace,
        tool_filter: set[str] | None = None,
        allowed_roles: list[str] | None = None,
    ) -> list[ToolDefinition]:
        self.register_calls.append(
            {
                "connection": conn.name,
                "tool_filter": tool_filter,
                "allowed_roles": allowed_roles,
            }
        )
        return []


class MCPAccessScopeTests(unittest.IsolatedAsyncioTestCase):
    def test_empty_scopes_preserve_global_behavior(self) -> None:
        server = MCPServerConfig(name="knowledge")
        config = OPCConfig()
        engine = OPCEngine(config=config, project_id="research")

        self.assertTrue(engine._mcp_server_matches_runtime_scope(server))

    def test_organization_and_project_must_both_match(self) -> None:
        server = MCPServerConfig(
            name="knowledge",
            organizations=["org-a"],
            projects=["research"],
        )
        config = OPCConfig()
        config.org.organization_id = "org-a"

        self.assertTrue(
            OPCEngine(config=config, project_id="research")
            ._mcp_server_matches_runtime_scope(server)
        )
        self.assertFalse(
            OPCEngine(config=config, project_id="other")
            ._mcp_server_matches_runtime_scope(server)
        )

    async def test_role_scope_filters_schema_and_dispatch(self) -> None:
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="graph_read",
                description="Read the graph",
                parameters={},
                func=_ok_tool,
                allowed_roles=["researcher"],
            )
        )

        self.assertEqual(
            [schema["name"] for schema in registry.get_schemas(role_id="researcher")],
            ["graph_read"],
        )
        self.assertEqual(registry.get_schemas(role_id="marketing"), [])

        trusted_task = Task(assigned_to="marketing")
        trusted_task._tool_scope_role_id = "researcher"
        allowed = await registry.execute(
            "graph_read",
            {},
            task=trusted_task,
        )
        denied = await registry.execute(
            "graph_read",
            {},
            task=Task(assigned_to="marketing"),
        )
        missing_context = await registry.execute("graph_read", {})

        self.assertTrue(allowed["success"])
        self.assertFalse(denied["success"])
        self.assertFalse(missing_context["success"])

    async def test_task_assignment_cannot_forge_runtime_role(self) -> None:
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="graph_read",
                description="Read the graph",
                parameters={},
                func=_ok_tool,
                allowed_roles=["researcher"],
            )
        )

        result = await registry.execute(
            "graph_read",
            {},
            task=Task(assigned_to="researcher"),
        )

        self.assertFalse(result["success"])

    async def test_runtime_owned_role_overrides_task_assignment(self) -> None:
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="graph_read",
                description="Read the graph",
                parameters={},
                func=_ok_tool,
                allowed_roles=["researcher"],
            )
        )
        task = Task(assigned_to="marketing")
        task._tool_scope_role_id = "researcher"

        result = await registry.execute("graph_read", {}, task=task)

        self.assertTrue(result["success"])

    async def test_mismatched_server_is_not_started(self) -> None:
        config = OPCConfig()
        config.org.organization_id = "org-a"
        config.system.mcp_servers = [
            MCPServerConfig(
                name="restricted",
                command=["server-command"],
                organizations=["org-b"],
            )
        ]
        engine = OPCEngine(config=config, project_id="research")
        manager = _FakeMCPManager()
        engine.mcp_manager = manager

        await engine._register_mcp_tools()

        self.assertEqual(manager.connect_calls, [])
        self.assertEqual(manager.register_calls, [])

    async def test_matching_server_passes_role_scope_to_tools(self) -> None:
        config = OPCConfig()
        config.org.organization_id = "org-a"
        config.system.mcp_servers = [
            MCPServerConfig(
                name="restricted",
                command=["server-command"],
                organizations=["org-a"],
                projects=["research"],
                roles=["researcher"],
            )
        ]
        engine = OPCEngine(config=config, project_id="research")
        manager = _FakeMCPManager()
        engine.mcp_manager = manager

        await engine._register_mcp_tools()

        self.assertEqual(len(manager.connect_calls), 1)
        self.assertEqual(manager.register_calls[0]["allowed_roles"], ["researcher"])


if __name__ == "__main__":
    unittest.main()

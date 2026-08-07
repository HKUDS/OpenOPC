"""Context loader — assembles all relevant context before routing."""

from __future__ import annotations

from typing import Any

from opc.database.store import OPCStore
from opc.layer5_memory.memory_manager import MemoryManager
from opc.layer5_memory.preference import PreferenceManager
from opc.layer5_memory.secretary_policy import SecretaryPolicyManager
from opc.layer5_memory.capability_manager import CapabilityManager
from opc.layer5_memory.skill_library import SkillLibrary
from opc.layer3_agent.adapters.registry import AdapterRegistry
from opc.layer2_organization.org_engine import OrgEngine


class LoadedContext:
    """Container for all context assembled for a task."""

    def __init__(self) -> None:
        self.preferences: dict[str, Any] = {}
        self.memory: str = ""
        self.project_memory: str = ""
        self.session_memory: str = ""
        self.skills_context: str = ""
        self.available_external_agents: list[str] = []
        self.external_agent_profiles: list[dict[str, Any]] = []
        self.company_profile: str = "corporate"
        self.company_profiles: list[str] = ["corporate", "custom"]
        self.company_profile_descriptions: dict[str, str] = {}
        self.autonomy_preferences: dict[str, Any] = {}
        self.autonomy_stats: dict[str, Any] = {}
        self.external_sessions: list[dict[str, Any]] = []
        self.session_execution_defaults: dict[str, Any] = {}
        self.project_id: str | None = None
        self.capability_catalog_summary: str = ""
        self.secretary_context: str = ""
        self.default_channel: str = "cli"
        self.origin_chat_id: str = ""
        self.origin_thread_id: str = ""

    def has_capable_external_agent(self, domains: list[str]) -> bool:
        _ = domains
        return bool(self.available_external_agents)


class ContextLoader:
    """Loads all relevant context for a task before routing."""

    def __init__(
        self,
        memory: MemoryManager,
        preferences: PreferenceManager,
        secretary_policies: SecretaryPolicyManager,
        skills: SkillLibrary,
        capability_manager: CapabilityManager,
        adapter_registry: AdapterRegistry,
        org_engine: OrgEngine,
        store: OPCStore,
    ) -> None:
        self.memory = memory
        self.preferences = preferences
        self.secretary_policies = secretary_policies
        self.skills = skills
        self.capability_manager = capability_manager
        self.adapters = adapter_registry
        self.org_engine = org_engine
        self.store = store

    async def load(
        self,
        project_id: str | None = None,
        session_id: str | None = None,
        domains: list[str] | None = None,
        *,
        include_project_knowledge: bool = False,
    ) -> LoadedContext:
        ctx = LoadedContext()
        ctx.project_id = project_id
        ctx.preferences = {}
        ctx.autonomy_preferences = {}
        ctx.secretary_context = ""
        if session_id:
            session = await self.store.get_session(session_id)
            if session:
                defaults = session.metadata.get("execution_defaults", {})
                if isinstance(defaults, dict):
                    ctx.session_execution_defaults = dict(defaults)
        ctx.project_memory = await self.memory.build_memory_context(
            project_id=project_id,
            session_id=None,
            include_project_knowledge=include_project_knowledge,
        )
        if session_id:
            maybe_compact = getattr(self.memory, "maybe_compact_session_history", None)
            if callable(maybe_compact):
                await maybe_compact(session_id, project_id=project_id)
        ctx.session_memory = (
            await self.memory.build_session_prompt_context(
                session_id,
                include_latest_user_turn=False,
            )
            if session_id
            else ""
        )
        ctx.memory = "\n\n".join(part for part in (ctx.project_memory, ctx.session_memory) if part)
        # Pre-routing catalog: we don't yet know which execution mode
        # the user will land in, so mode-restricted skills (e.g. a
        # collaboration playbook scoped to company_mode) are hidden
        # from this top-level catalog. They surface later via the
        # per-turn prompt harness, which does know the execution mode.
        ctx.skills_context = self.skills.build_skills_summary(project_id, execution_mode=None)
        ctx.capability_catalog_summary = self.capability_manager.build_catalog_summary()
        ctx.available_external_agents = self.adapters.list_available()
        ctx.external_agent_profiles = self.adapters.describe_all()
        ctx.company_profile = self.org_engine.get_company_profile()
        ctx.company_profiles = list(self.org_engine.config.org.company_profiles)
        ctx.company_profile_descriptions = self.org_engine.get_company_profile_descriptions()
        ctx.autonomy_stats = await self.store.get_autonomy_stats(project_id=project_id)
        sessions = []
        for agent in ctx.available_external_agents:
            session = await self.store.get_external_session(agent_type=agent, project_id=project_id or "default")
            if session:
                sessions.append({
                    "agent_type": session.agent_type,
                    "session_id": session.session_id,
                    "run_mode": session.run_mode,
                    "status": session.status,
                    "updated_at": session.updated_at.isoformat(),
                    "last_activity_at": session.metadata.get("last_activity_at", ""),
                    "activity_count": session.metadata.get("activity_count", 0),
                    "pid": session.metadata.get("pid"),
                })
        ctx.external_sessions = sessions
        return ctx

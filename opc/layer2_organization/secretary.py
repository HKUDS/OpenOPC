"""Secretary service for long-term policy capture and lightweight governance."""

from __future__ import annotations

import json
import uuid
from typing import Any

from loguru import logger

from opc.database.store import OPCStore
from opc.layer5_memory.memory_manager import MemoryManager
from opc.layer5_memory.preference import PreferenceManager
from opc.layer5_memory.secretary_policy import SecretaryPolicyManager
from opc.layer5_memory.skill_importer import ExternalSkillImporter, SkillImportError
from opc.layer5_memory.skill_library import SkillLibrary
from opc.llm.provider import LLMProvider
from opc.llm.retry import LLMRetryError, call_llm_json_with_retry


class SecretaryService:
    """Direct secretary interface with long-term memory and policy updates."""

    def __init__(
        self,
        llm: LLMProvider,
        store: OPCStore,
        memory: MemoryManager,
        preferences: PreferenceManager,
        skills: SkillLibrary,
        policies: SecretaryPolicyManager,
    ) -> None:
        self.llm = llm
        self.store = store
        self.memory = memory
        self.preferences = preferences
        self.skills = skills
        self.policies = policies
        self.skill_importer = ExternalSkillImporter(skill_library=skills, policies=policies)

    async def handle_message(
        self,
        content: str,
        *,
        project_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        secretary_session_id = session_id or str(uuid.uuid4())
        await self.memory.ensure_session(
            secretary_session_id,
            project_id=project_id or "default",
            title=(content[:120] or "Secretary Session").strip(),
            mode="primary",
            metadata={"interface": "secretary"},
        )
        await self.memory.record_user_turn(secretary_session_id, content, project_id=project_id or "default")

        prompt = await self._build_prompt(content, project_id=project_id, session_id=secretary_session_id)
        raw_fallback_text = ""
        try:
            parsed = await call_llm_json_with_retry(
                self.llm,
                system=self._system_prompt(),
                payload=prompt,
                task_type="quick_tasks",
                label="secretary",
            )
        except LLMRetryError as exc:
            logger.warning(
                f"Secretary LLM returned invalid JSON after retries: {exc}; "
                "falling back to plain-text echo."
            )
            raw_fallback_text = str(exc.last_raw or "").strip()
            parsed = {"response": raw_fallback_text, "actions": []}
        applied_updates: list[str] = []
        applied_actions = await self._apply_actions(parsed.get("actions", []), project_id=project_id)

        reply = str(parsed.get("response", "")).strip() or raw_fallback_text
        if applied_updates:
            reply += "\n\nApplied secretary updates:\n" + "\n".join(f"- {item}" for item in applied_updates)
        if applied_actions:
            reply += "\n\nApplied secretary actions:\n" + "\n".join(f"- {item}" for item in applied_actions)
        await self.memory.record_assistant_turn(
            secretary_session_id,
            reply,
            project_id=project_id or "default",
            metadata={"kind": "secretary_reply"},
        )
        return {
            "response": reply,
            "session_id": secretary_session_id,
            "applied_updates": applied_updates,
            "applied_actions": applied_actions,
        }

    async def list_sessions(self, project_id: str | None, limit: int = 20) -> list[Any]:
        sessions = await self.store.list_sessions(project_id=project_id or "default", parent_session_id=None, limit=limit * 3)
        return [item for item in sessions if item.metadata.get("interface") == "secretary"][:limit]

    def describe_policies(self, project_id: str | None = None) -> str:
        return self.policies.summarize_policies(project_id=project_id)

    async def _build_prompt(self, content: str, project_id: str | None, session_id: str) -> str:
        policy_summary = self.policies.summarize_policies(project_id=project_id)
        project_knowledge = await self.memory.build_project_knowledge_context(project_id=project_id)
        maybe_compact = getattr(self.memory, "maybe_compact_session_history", None)
        if callable(maybe_compact):
            await maybe_compact(session_id, project_id=project_id)
        session_history = await self.memory.build_session_prompt_context(
            session_id,
            include_latest_user_turn=False,
        )
        recent_events = await self.store.get_events(limit=12)
        event_lines: list[str] = []
        for event in reversed(recent_events[-8:]):
            payload = str(event.get("payload", ""))
            event_lines.append(f"- {event.get('event_type', '')}: {payload}")
        skill_names = [skill.name for skill in self.skills.list_skills()]
        current_preferences = self.preferences.load_merged(project_id=project_id)
        context = {
            "project_id": project_id or "default",
            "user_message": content,
            "current_secretary_policies": policy_summary,
            "current_preferences": {
                "communication_style": current_preferences.get("communication_style", ""),
                "preferred_language": current_preferences.get("preferred_language", ""),
                "decision_preferences": current_preferences.get("decision_preferences", {}),
            },
            "project_knowledge": project_knowledge,
            "secretary_session_history": session_history,
            "recent_structured_events": event_lines,
            "available_skill_names": skill_names[:80],
        }
        return json.dumps(context, ensure_ascii=False)

    def _system_prompt(self) -> str:
        prompt = (
            "You are the long-term secretary of the OPC system.\n"
            "Your job is to answer as a practical assistant. Durable memory and policy updates are handled by agents through the memory skill, not by the secretary.\n"
            "Important constraints:\n"
            "- Do not create memory notes, authorization rules, workspace guardrails, skill injection rules, or preferences.\n"
            "- Use actions only for explicit skill imports.\n"
            "- Return strict JSON only.\n\n"
            "JSON schema:\n"
            "{\n"
            '  "response": "assistant reply",\n'
            '  "actions": [\n'
            "    {\n"
            '      "kind": "import_skill",\n'
            '      "scope": "project",\n'
            '      "source": "clawhub" | "path",\n'
            '      "query": "natural language search terms or exact slug",\n'
            '      "slug": "exact-skill-slug-if-known",\n'
            '      "path": "/absolute/path/to/downloaded/skill/folder",\n'
            '      "domains": ["coding"],\n'
            '      "enable": true,\n'
            '      "rationale": "why this import is needed"\n'
            "    }\n"
            "  ]\n"
            "}"
        )
        return prompt

    def _parse_response(self, raw: str) -> dict[str, Any]:
        text = raw.strip()
        if text.startswith("```"):
            parts = text.split("\n", 1)
            text = parts[1] if len(parts) == 2 else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except Exception as e:
            logger.debug(f"Secretary JSON parse failed: {e}")
        return {"response": raw.strip(), "actions": []}

    async def _apply_updates(self, updates: list[Any], project_id: str | None) -> list[str]:
        _ = (updates, project_id)
        return []

    async def _apply_actions(self, actions: list[Any], project_id: str | None) -> list[str]:
        applied: list[str] = []
        for action in actions:
            if not isinstance(action, dict):
                continue
            kind = str(action.get("kind", "")).strip()
            if kind == "update_preferences":
                applied.append("skipped preference update because secretary memory writes are disabled")
                continue

            if kind != "import_skill":
                continue
            if not project_id:
                applied.append("skipped skill import because the secretary needs a project context")
                continue
            source = str(action.get("source", "clawhub")).strip().lower() or "clawhub"
            query = str(action.get("query", "")).strip()
            slug = str(action.get("slug", "")).strip()
            path = str(action.get("path", "")).strip()
            if source == "clawhub" and not query and not slug:
                applied.append("skipped skill import because no skill query or slug was provided")
                continue
            if source in {"path", "directory", "local"} and not path:
                applied.append("skipped skill import because no local skill path was provided")
                continue

            domains = [str(item).strip() for item in action.get("domains", []) if str(item).strip()]
            enable = bool(action.get("enable", True))
            try:
                result = await self.skill_importer.import_skill(
                    project_id=project_id,
                    source=source,
                    query=query,
                    slug=slug,
                    path=path,
                    domains=domains,
                    enable=enable,
                )
                summary = f"imported skill `{result.skill_name}` and made it available in project `{project_id}`"
                if result.enabled_domains:
                    summary += f"; auto-injected for {', '.join(result.enabled_domains)}"
                applied.append(summary)
            except SkillImportError as exc:
                applied.append(f"skill import failed: {exc}")
        return applied

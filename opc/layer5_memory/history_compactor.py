"""Persistent history compaction for session and employee memory."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from loguru import logger

from opc.core.models import (
    AgentCompactionRecord,
    AgentMemorySnapshotRecord,
    SessionCompactionRecord,
    SessionMemorySnapshotRecord,
)


class HistoryCompactor:
    """Compacts persisted history into summary + memory snapshots."""

    _COMPACTION_MESSAGE_CHAR_BUDGET = 4_000
    _COMPACTION_TRUNCATION_MARKER = "[history compaction input truncated]"
    _RETRY_TRUNCATION_MARKER = "[history compaction retry truncated]"

    def __init__(
        self,
        *,
        llm: Any | None,
        store: Any | None,
        memory_manager: Any,
        task_type: str = "quick_tasks",
        compression_threshold: float = 0.85,
    ) -> None:
        self.llm = llm
        self.store = store
        self.memory_manager = memory_manager
        self.task_type = task_type
        self.compression_threshold = compression_threshold

    async def summarize_runtime_history(
        self,
        *,
        project_id: str,
        session_id: str,
        messages: list[dict[str, Any]],
        existing_summary: str = "",
    ) -> str:
        """Summarize in-memory runtime messages for durable context compaction.

        Used by NativeRuntimeV2 when live context reaches the hard threshold:
        the returned summary replaces the folded span of the message list.
        Raises on non-recoverable LLM errors so the caller can count failures.
        """
        if not messages:
            return ""
        if not self.llm:
            return self._fallback_session_summary(messages, existing_summary)["history_summary"]
        payload = {
            "project_id": project_id,
            "session_id": session_id,
            "existing_summary": existing_summary,
            "messages": messages,
        }
        raw = await self._simple_chat_with_retry(
            payload=payload,
            system=(
                "You are compacting the live working context of an agent that must "
                "continue its task seamlessly from your output.\n"
                "Return strict JSON with a single key `history_summary`.\n"
                "`history_summary` must be detailed markdown with sections:\n"
                "1. Primary Request and Intent\n"
                "2. Key Technical Concepts\n"
                "3. Files and Code Sections\n"
                "4. Errors and Fixes (especially user corrections)\n"
                "5. Problem Solving\n"
                "6. All User Messages\n"
                "7. Pending Tasks\n"
                "8. Current Work\n"
                "9. Next Step\n"
                "Quote exact identifiers, paths, commands, and values the agent will "
                "need to continue; do not invent details."
            ),
        )
        parsed = self._parse_json_response(raw)
        summary = str((parsed or {}).get("history_summary", "")).strip()
        if summary:
            return summary
        return self._fallback_session_summary(messages, existing_summary)["history_summary"]

    async def maybe_compact_session(
        self,
        *,
        project_id: str,
        session_id: str,
        force: bool = False,
    ) -> bool:
        if not self.llm or not self.store:
            return False
        visible_items = await self.memory_manager._get_visible_session_transcript(session_id)
        if not visible_items:
            return False
        raw_items = [item for item in visible_items if not getattr(item["message"], "summary_flag", False)]
        if not raw_items:
            return False
        visible_messages = self._items_to_messages(visible_items)
        if not self._should_compact(visible_messages, force=force):
            return False
        compact_items, boundary_message_id = self._select_compaction_items(raw_items, force=force)
        if not compact_items or not boundary_message_id:
            return False
        messages = self._items_to_messages(
            compact_items,
            per_message_budget=self._COMPACTION_MESSAGE_CHAR_BUDGET,
        )

        existing = await self.store.get_latest_session_memory_snapshot(session_id)
        result = await self._summarize_session(
            project_id=project_id,
            session_id=session_id,
            messages=messages,
            existing_memory=(existing.memory_text if existing else ""),
            existing_summary=(existing.summary_text if existing else ""),
        )
        summary_message = await self.memory_manager.append_session_message(
            session_id=session_id,
            role="assistant",
            text=result["history_summary"],
            project_id=project_id,
            summary_flag=True,
            parent_message_id=boundary_message_id,
            metadata={
                "kind": "session_history_summary",
                "summary_scope": "session",
                "skip_compaction": True,
            },
        )
        if not summary_message:
            return False

        await self.store.save_session_compaction(
            SessionCompactionRecord(
                session_id=session_id,
                compaction_message_id=summary_message.message_id,
                source_boundary_message_id=boundary_message_id,
                metadata={
                    "project_id": project_id,
                    "raw_message_count": len(compact_items),
                    "summary_scope": "session",
                },
            )
        )
        await self.store.save_session_memory_snapshot(
            SessionMemorySnapshotRecord(
                project_id=project_id,
                session_id=session_id,
                summary_message_id=summary_message.message_id,
                source_boundary_message_id=boundary_message_id,
                summary_text=result["history_summary"],
                memory_text=result["memory_summary"],
                metadata={
                    "summary_scope": "session",
                    "raw_message_count": len(compact_items),
                },
            )
        )
        await self.memory_manager.update_session_summary(session_id, result["history_summary"])
        return True

    async def maybe_compact_agent(
        self,
        *,
        project_id: str,
        session_id: str,
        employee_id: str,
        role_id: str = "",
        force: bool = False,
    ) -> bool:
        if not self.llm or not self.store or not employee_id:
            return False
        visible_items = await self.memory_manager._get_visible_agent_transcript(
            project_id=project_id,
            session_id=session_id,
            employee_id=employee_id,
        )
        if not visible_items:
            return False
        raw_items = [item for item in visible_items if not getattr(item["message"], "summary_flag", False)]
        if not raw_items:
            return False
        visible_messages = self._items_to_messages(visible_items)
        if not self._should_compact(visible_messages, force=force):
            return False
        compact_items, boundary_message_id = self._select_compaction_items(raw_items, force=force)
        if not compact_items or not boundary_message_id:
            return False
        messages = self._items_to_messages(
            compact_items,
            per_message_budget=self._COMPACTION_MESSAGE_CHAR_BUDGET,
        )

        existing = await self.store.get_agent_memory_snapshot(
            project_id=project_id,
            session_id=session_id,
            employee_id=employee_id,
            memory_kind="process",
            memory_scope="session",
        )
        result = await self._summarize_agent_process(
            project_id=project_id,
            session_id=session_id,
            employee_id=employee_id,
            role_id=role_id,
            messages=messages,
            existing_memory=(existing.memory_text if existing else ""),
            existing_summary=(existing.summary_text if existing else ""),
        )
        summary_message = await self.memory_manager.append_session_message(
            session_id=session_id,
            role="assistant",
            text=result["history_summary"],
            project_id=project_id,
            summary_flag=True,
            parent_message_id=boundary_message_id,
            metadata={
                "kind": "agent_history_summary",
                "summary_scope": "agent",
                "employee_id": employee_id,
                "role_id": role_id,
                "skip_compaction": True,
            },
        )
        if not summary_message:
            return False

        await self.store.save_agent_compaction(
            AgentCompactionRecord(
                project_id=project_id,
                session_id=session_id,
                employee_id=employee_id,
                role_id=role_id,
                compaction_message_id=summary_message.message_id,
                source_boundary_message_id=boundary_message_id,
                metadata={
                    "summary_scope": "agent",
                    "raw_message_count": len(compact_items),
                },
            )
        )
        await self.store.save_agent_memory_snapshot(
            AgentMemorySnapshotRecord(
                project_id=project_id,
                session_id=session_id,
                employee_id=employee_id,
                role_id=role_id,
                memory_scope="session",
                memory_kind="process",
                summary_message_id=summary_message.message_id,
                source_boundary_message_id=boundary_message_id,
                summary_text=result["history_summary"],
                memory_text=result["memory_summary"],
                metadata={
                    "summary_scope": "agent",
                    "raw_message_count": len(compact_items),
                },
            )
        )
        return True

    async def finalize_agent_memory(
        self,
        *,
        project_id: str,
        session_id: str,
        employee_id: str,
        role_id: str,
        process_memory: str,
        reflection_payload: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.llm:
            return self._fallback_final_agent_memory(process_memory, reflection_payload)
        prompt_payload = {
            "project_id": project_id,
            "session_id": session_id,
            "employee_id": employee_id,
            "role_id": role_id,
            "process_memory": process_memory,
            "reflection": reflection_payload,
        }
        raw = await self.llm.simple_chat(
            prompt=json.dumps(prompt_payload, ensure_ascii=False),
            system=(
                "You are finalizing employee memory for a multi-agent runtime.\n"
                "Return strict JSON with keys `summary_text`, `memory_text`, and `metadata`.\n"
                "`memory_text` must be concise markdown with sections:\n"
                "## Effective Patterns\n## Watchouts\n## Preferred Tools\n## Reviewer Preferences\n## Reusable Checklist\n"
                "`metadata` must contain arrays with keys `effective_patterns`, `watchouts`, "
                "`preferred_tools`, `reviewer_preferences`, `reusable_checklist`.\n"
                "Merge the process memory with the reflection, remove duplication, and keep only durable guidance."
            ),
            task_type=self.task_type,
        )
        parsed = self._parse_json_response(raw)
        if not parsed:
            return self._fallback_final_agent_memory(process_memory, reflection_payload)
        metadata = parsed.get("metadata", {})
        return {
            "summary_text": str(parsed.get("summary_text", "")).strip() or str(parsed.get("memory_text", "")).strip(),
            "memory_text": str(parsed.get("memory_text", "")).strip() or process_memory.strip(),
            "metadata": metadata if isinstance(metadata, dict) else {},
        }

    def _get_token_threshold(self, *, reserve_tokens: int = 0) -> int | None:
        if not self.llm:
            return None
        context_limit = self.llm.get_context_window(task_type=self.task_type)
        if context_limit is None:
            return None
        threshold = int(context_limit * self.compression_threshold)
        if reserve_tokens:
            threshold = max(0, threshold - reserve_tokens)
        return threshold


    def _is_context_overflow_error(self, error: Exception) -> bool:
        detector = getattr(self.llm, "is_context_overflow_error", None)
        if callable(detector):
            try:
                return bool(detector(error))
            except Exception:
                return False
        return False

    @staticmethod
    def _truncate_messages_for_retry(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(messages) <= 1:
            return messages
        drop_count = max(1, len(messages) // 5)
        if drop_count >= len(messages):
            drop_count = len(messages) - 1
        return messages[drop_count:]

    @classmethod
    def _truncate_message_content(cls, content: str, *, budget: int, marker: str) -> str:
        if len(content) <= budget:
            return content
        clipped = max(120, budget - len(marker) - 1)
        return content[:clipped].rstrip() + "\n" + marker

    @classmethod
    def _compact_messages_for_retry(
        cls,
        messages: list[dict[str, Any]],
        *,
        budget: int,
    ) -> list[dict[str, Any]]:
        compacted: list[dict[str, Any]] = []
        for message in messages:
            compacted.append({
                **message,
                "content": cls._truncate_message_content(
                    str(message.get("content", "") or ""),
                    budget=budget,
                    marker=cls._RETRY_TRUNCATION_MARKER,
                ),
            })
        return compacted

    async def _simple_chat_with_retry(
        self,
        *,
        payload: dict[str, Any],
        system: str,
    ) -> str:
        retries = 0
        working_payload = dict(payload)
        while True:
            try:
                return await self.llm.simple_chat(
                    prompt=json.dumps(working_payload, ensure_ascii=False),
                    system=system,
                    task_type=self.task_type,
                )
            except Exception as exc:
                messages = list(working_payload.get("messages", []) or [])
                if retries >= 3 or not self._is_context_overflow_error(exc) or not messages:
                    raise
                retries += 1
                retry_budget = max(1_200, 4_000 // (2 ** (retries - 1)))
                retry_messages = self._compact_messages_for_retry(messages, budget=retry_budget)
                if len(retry_messages) > 1:
                    retry_messages = self._truncate_messages_for_retry(retry_messages)
                working_payload["messages"] = retry_messages
                logger.debug(
                    "History compactor retrying after context overflow with "
                    f"{len(working_payload['messages'])} messages preserved and budget={retry_budget} chars."
                )

    def _should_compact(self, messages: list[dict[str, Any]], *, force: bool = False) -> bool:
        if force:
            return True
        if not messages or not self.llm:
            return False
        counted_tokens = self.llm.count_input_tokens(messages, task_type=self.task_type)
        threshold_tokens = self._get_token_threshold()
        if counted_tokens is None or threshold_tokens is None:
            return False
        return counted_tokens >= threshold_tokens

    def _select_compaction_items(
        self,
        raw_items: list[dict[str, Any]],
        *,
        force: bool = False,
    ) -> tuple[list[dict[str, Any]], str]:
        if not raw_items:
            return [], ""
        if force or not self.llm:
            return raw_items, raw_items[-1]["message"].message_id

        threshold_tokens = self._get_token_threshold()
        if threshold_tokens is None:
            return [], ""

        for keep_start in range(len(raw_items)):
            tail_messages = self._items_to_messages(raw_items[keep_start:])
            tail_tokens = self.llm.count_input_tokens(tail_messages, task_type=self.task_type)
            if tail_tokens is None:
                continue
            if tail_tokens < threshold_tokens:
                compact_items = raw_items if keep_start == 0 else raw_items[:keep_start]
                return compact_items, compact_items[-1]["message"].message_id

        return raw_items, raw_items[-1]["message"].message_id

    def _items_to_messages(
        self,
        items: list[dict[str, Any]],
        *,
        per_message_budget: int | None = None,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for item in items:
            message = item["message"]
            content = self.memory_manager._render_session_parts(item["parts"]).strip()
            if not content:
                continue
            if per_message_budget:
                content = self._truncate_message_content(
                    content,
                    budget=per_message_budget,
                    marker=self._COMPACTION_TRUNCATION_MARKER,
                )
            role = "user" if message.role == "user" else "assistant"
            messages.append({"role": role, "content": content})
        return messages

    async def _summarize_session(
        self,
        *,
        project_id: str,
        session_id: str,
        messages: list[dict[str, Any]],
        existing_memory: str,
        existing_summary: str,
    ) -> dict[str, str]:
        if not self.llm:
            return self._fallback_session_summary(messages, existing_memory)
        payload = {
            "project_id": project_id,
            "session_id": session_id,
            "existing_memory": existing_memory,
            "existing_summary": existing_summary,
            "messages": messages,
        }
        raw = await self._simple_chat_with_retry(
            payload=payload,
            system=(
                "You are compacting persisted session history.\n"
                "Return strict JSON with keys `history_summary` and `memory_summary`.\n"
                "`history_summary` should help another agent continue the session after restart.\n"
                "`memory_summary` should be concise markdown with sections:\n"
                "## Primary Goal\n## Active Rules\n## Key Progress\n## Current State\n## Open Risks\n"
                "Merge with existing memory and remove duplication."
            ),
        )
        parsed = self._parse_json_response(raw)
        if not parsed:
            return self._fallback_session_summary(messages, existing_memory)
        return {
            "history_summary": str(parsed.get("history_summary", "")).strip() or self._fallback_session_summary(messages, existing_memory)["history_summary"],
            "memory_summary": str(parsed.get("memory_summary", "")).strip() or self._fallback_session_summary(messages, existing_memory)["memory_summary"],
        }

    async def _summarize_agent_process(
        self,
        *,
        project_id: str,
        session_id: str,
        employee_id: str,
        role_id: str,
        messages: list[dict[str, Any]],
        existing_memory: str,
        existing_summary: str,
    ) -> dict[str, str]:
        if not self.llm:
            return self._fallback_agent_summary(messages, existing_memory)
        payload = {
            "project_id": project_id,
            "session_id": session_id,
            "employee_id": employee_id,
            "role_id": role_id,
            "existing_memory": existing_memory,
            "existing_summary": existing_summary,
            "messages": messages,
        }
        raw = await self._simple_chat_with_retry(
            payload=payload,
            system=(
                "You are compacting employee-level process history.\n"
                "Return strict JSON with keys `history_summary` and `memory_summary`.\n"
                "`history_summary` should summarize what this employee already did in this session.\n"
                "`memory_summary` should be concise markdown with sections:\n"
                "## Effective Patterns\n## Watchouts\n## Current Progress\n## Current State\n"
                "Keep it durable, specific, and deduplicated."
            ),
        )
        parsed = self._parse_json_response(raw)
        if not parsed:
            return self._fallback_agent_summary(messages, existing_memory)
        return {
            "history_summary": str(parsed.get("history_summary", "")).strip() or self._fallback_agent_summary(messages, existing_memory)["history_summary"],
            "memory_summary": str(parsed.get("memory_summary", "")).strip() or self._fallback_agent_summary(messages, existing_memory)["memory_summary"],
        }

    def _fallback_session_summary(self, messages: list[dict[str, Any]], existing_memory: str) -> dict[str, str]:
        snippets = [str(item.get("content", "")).strip() for item in messages if str(item.get("content", "")).strip()]
        summary = "\n".join(f"- {snippet}" for snippet in snippets[-6:]) or "- No prior details captured."
        memory_parts = [
            "## Primary Goal",
            f"- {snippets[0]}" if snippets else "- (unknown)",
            "",
            "## Active Rules",
            "- Reuse durable constraints from earlier turns.",
            "",
            "## Key Progress",
            *([f"- {snippet}" for snippet in snippets[-4:]] or ["- (none)"]),
            "",
            "## Current State",
            f"- Existing memory length: {len(existing_memory.strip())} characters",
            "",
            "## Open Risks",
            "- Re-check older transcript if the summary omits key details.",
        ]
        return {
            "history_summary": summary.strip(),
            "memory_summary": "\n".join(memory_parts).strip(),
        }

    def _fallback_agent_summary(self, messages: list[dict[str, Any]], existing_memory: str) -> dict[str, str]:
        snippets = [str(item.get("content", "")).strip() for item in messages if str(item.get("content", "")).strip()]
        memory_parts = [
            "## Effective Patterns",
            *([f"- {snippet}" for snippet in snippets[-3:]] or ["- (none yet)"]),
            "",
            "## Watchouts",
            "- Avoid repeating failed or already-compacted paths without new evidence.",
            "",
            "## Current Progress",
            *([f"- {snippet}" for snippet in snippets[-2:]] or ["- (none)"]),
            "",
            "## Current State",
            f"- Existing process memory length: {len(existing_memory.strip())} characters",
        ]
        return {
            "history_summary": "\n".join(f"- {snippet}" for snippet in snippets[-5:]) or "- No agent history captured.",
            "memory_summary": "\n".join(memory_parts).strip(),
        }

    def _fallback_final_agent_memory(
        self,
        process_memory: str,
        reflection_payload: dict[str, Any],
    ) -> dict[str, Any]:
        what_worked = [str(item).strip() for item in reflection_payload.get("what_worked", []) if str(item).strip()]
        watchouts = [str(item).strip() for item in reflection_payload.get("mistakes_to_avoid", []) if str(item).strip()]
        preferred_tools = [str(item).strip() for item in reflection_payload.get("tool_preferences", []) if str(item).strip()]
        reviewer_preferences = [str(item).strip() for item in reflection_payload.get("reviewer_preferences", []) if str(item).strip()]
        checklist = [str(item).strip() for item in reflection_payload.get("reusable_checklist", []) if str(item).strip()]
        parts = [
            "## Effective Patterns",
            *([f"- {item}" for item in what_worked[:6]] or ["- (none)"]),
            "",
            "## Watchouts",
            *([f"- {item}" for item in watchouts[:6]] or ["- (none)"]),
            "",
            "## Preferred Tools",
            *([f"- {item}" for item in preferred_tools[:6]] or ["- (none)"]),
            "",
            "## Reviewer Preferences",
            *([f"- {item}" for item in reviewer_preferences[:6]] or ["- (none)"]),
            "",
            "## Reusable Checklist",
            *([f"- {item}" for item in checklist[:6]] or ["- (none)"]),
        ]
        summary_text = str(reflection_payload.get("project_summary", "")).strip() or process_memory.strip()
        return {
            "summary_text": summary_text,
            "memory_text": "\n".join(parts).strip(),
            "metadata": {
                "effective_patterns": what_worked[:6],
                "watchouts": watchouts[:6],
                "preferred_tools": preferred_tools[:6],
                "reviewer_preferences": reviewer_preferences[:6],
                "reusable_checklist": checklist[:6],
            },
        }

    def _parse_json_response(self, raw: str) -> dict[str, Any]:
        text = raw.strip()
        if text.startswith("```"):
            parts = text.split("\n", 1)
            text = parts[1] if len(parts) == 2 else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else {}
        except Exception:
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                try:
                    data = json.loads(text[start : end + 1])
                    return data if isinstance(data, dict) else {}
                except Exception as e:
                    logger.warning(f"Failed to parse compaction JSON: {e}")
        return {}

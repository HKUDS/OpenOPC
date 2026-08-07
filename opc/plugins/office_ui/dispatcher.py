"""Dispatcher — UI session-level intent classifier and lightweight router.

Sits between user messages and engine.process_message(). For heavy task
requests it delegates to the engine pipeline; for status queries, simple
conversation, and session control it responds directly without creating Tasks.

This is a UI-only component (CLI does not need it).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, TYPE_CHECKING

from loguru import logger
from opc.layer2_organization.work_item_transition import apply_task_status_transition

if TYPE_CHECKING:
    from opc.engine import OPCEngine
    from opc.plugins.office_ui.chat_store import ChatStore


class Intent(str, Enum):
    TASK_REQUEST = "task_request"      # User wants something done → full engine pipeline
    STATUS_QUERY = "status_query"      # User asks about task/session status
    CONVERSATION = "conversation"      # Chitchat, clarification, general Q&A
    SESSION_CTRL = "session_ctrl"      # Cancel, rename, switch mode, etc.


@dataclass
class DispatchResult:
    route: str          # "engine" | "direct"
    intent: Intent
    response: str = ""  # Only used when route == "direct"


# ── Classification prompt (kept minimal for latency) ─────────────────────

_CLASSIFY_SYSTEM = (
    "You are a message classifier for an AI agent system. "
    "Given a user message, classify it into exactly ONE of these intents:\n\n"
    "- TASK_REQUEST: The user wants something done — coding, research, file operations, "
    "building something, fixing a bug, writing content, or any actionable work.\n"
    "- STATUS_QUERY: The user asks about status — \"is it done?\", \"what happened?\", "
    "\"how many tasks?\", \"show progress\".\n"
    "- CONVERSATION: Chitchat, greetings, clarification, general questions about the "
    "system, or follow-up that doesn't request new work.\n"
    "- SESSION_CTRL: The user wants to control the session — cancel a task, rename, "
    "change execution mode.\n\n"
    "Respond with a single JSON object: {\"intent\": \"<INTENT>\"}\n"
    "If unsure, default to TASK_REQUEST."
)

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


class Dispatcher:
    """UI session dispatcher — classifies intent and routes accordingly."""

    def __init__(self, engine: "OPCEngine", chat_store: "ChatStore") -> None:
        self.engine = engine
        self.chat_store = chat_store

    async def handle(
        self,
        task_id: str,
        content: str,
        session_id: str | None = None,
        *,
        has_attachments: bool = False,
    ) -> DispatchResult:
        """Classify user intent and either route to engine or answer directly."""
        # Direct dispatcher replies cannot carry multimodal payloads, so any
        # message with persisted attachments must go through the engine path.
        if has_attachments:
            return DispatchResult(route="engine", intent=Intent.TASK_REQUEST)

        intent = await self._classify_intent(content, task_id, session_id)

        if intent == Intent.TASK_REQUEST:
            return DispatchResult(route="engine", intent=intent)

        if intent == Intent.STATUS_QUERY:
            answer = await self._answer_status_query(content, task_id)
            return DispatchResult(route="direct", intent=intent, response=answer)

        if intent == Intent.SESSION_CTRL:
            answer = await self._handle_session_control(content, task_id)
            return DispatchResult(route="direct", intent=intent, response=answer)

        # CONVERSATION — lightweight LLM response
        answer = await self._lightweight_converse(content, session_id)
        return DispatchResult(route="direct", intent=intent, response=answer)

    # ── Intent classification ────────────────────────────────────────────

    _TASK_VERBS = frozenset((
        "write", "fix", "create", "build", "implement", "add",
        "update", "delete", "refactor", "debug", "test", "deploy",
        "analyze", "search", "find", "generate", "design", "setup",
        "configure", "install", "remove", "migrate", "optimize",
        "review", "check", "run", "make", "edit", "modify",
        "list", "describe", "explain", "compare", "summarize",
        "calculate", "plan", "draft", "develop", "convert",
    ))

    _TASK_PHRASES = ("show me", "help me", "can you", "please ", "i need", "i want")
    _TASK_PREFIXES_ZH = (
        "帮我", "请", "请你", "麻烦", "我想", "我需要", "给我", "继续", "开始",
        "修复", "修一下", "写", "创建", "新建", "加上", "添加", "更新", "改",
        "修改", "删除", "重构", "调试", "测试", "部署", "分析", "搜索", "查",
        "查一下", "翻译", "解释", "总结", "概括", "比较", "生成", "设计",
        "实现", "运行", "看看", "检查", "排查", "review",
    )

    _STATUS_PATTERNS = (
        "is it done", "how many tasks", "what happened",
        "show progress", "task status", "what's the status",
        "are you done", "how is it going", "any updates",
    )
    _STATUS_PATTERNS_ZH = (
        "进度", "状态", "完成了吗", "做完了吗", "怎么样了", "有结果吗",
        "发生了什么", "最新进展", "更新一下", "看下状态", "现在到哪了",
    )

    _GREETINGS = frozenset((
        "hi", "hey", "hello", "sup", "yo", "thanks", "ok", "bye",
        "thank you", "good morning", "good evening",
    ))
    _GREETINGS_ZH = frozenset((
        "你好", "您好", "嗨", "哈喽", "谢谢", "多谢", "拜拜", "早上好", "晚上好", "在吗",
    ))
    _SESSION_CTRL_PREFIXES_ZH = ("取消", "停止", "中止", "终止", "算了", "别做了")

    # Replies that should always route to engine (checkpoint approval/denial)
    _CHECKPOINT_REPLY_TOKENS = frozenset((
        "y", "yes", "ok", "okay", "approve", "approved", "confirm",
        "continue", "proceed", "go", "n", "no", "deny", "denied",
        "reject", "rejected", "stop", "cancel", "abort",
    ))

    @staticmethod
    def _contains_cjk(content: str) -> bool:
        return bool(_CJK_RE.search(content))

    def _try_fast_classify(self, content: str) -> Intent | None:
        """Pattern-based fast-path: skip LLM for obvious intents."""
        stripped = content.strip()
        lower = stripped.lower()

        # Checkpoint reply tokens → always route to engine so checkpoint can be resolved
        first_word = lower.split(",")[0].split()[0] if lower.split() else ""
        if first_word in self._CHECKPOINT_REPLY_TOKENS:
            return Intent.TASK_REQUEST

        # Short greetings
        if len(stripped) <= 15 and lower in self._GREETINGS:
            return Intent.CONVERSATION
        if len(stripped) <= 12 and stripped in self._GREETINGS_ZH:
            return Intent.CONVERSATION

        # Long messages are almost always task requests
        if len(stripped) > 100:
            return Intent.TASK_REQUEST

        # Session control
        first_word = lower.split()[0] if lower.split() else ""
        if first_word in ("cancel", "stop", "abort"):
            return Intent.SESSION_CTRL
        if any(stripped.startswith(prefix) for prefix in self._SESSION_CTRL_PREFIXES_ZH):
            return Intent.SESSION_CTRL

        # Status query patterns
        if any(p in lower for p in self._STATUS_PATTERNS):
            return Intent.STATUS_QUERY
        if any(pattern in stripped for pattern in self._STATUS_PATTERNS_ZH):
            return Intent.STATUS_QUERY

        # Imperative verb → task request
        if first_word in self._TASK_VERBS:
            return Intent.TASK_REQUEST

        # Multi-word task phrases
        if any(lower.startswith(p) for p in self._TASK_PHRASES):
            return Intent.TASK_REQUEST

        # Common Chinese task phrasing should skip classifier latency and go
        # directly to the engine path, matching the snappy CLI experience.
        if any(stripped.startswith(prefix) for prefix in self._TASK_PREFIXES_ZH):
            return Intent.TASK_REQUEST
        if self._contains_cjk(stripped):
            return Intent.TASK_REQUEST

        return None  # Ambiguous → fall through to LLM

    async def _classify_intent(
        self, content: str, task_id: str, session_id: str | None
    ) -> Intent:
        """Use a fast LLM call to classify the user's intent."""
        # Fast-path: skip LLM for obvious intents
        fast = self._try_fast_classify(content)
        if fast is not None:
            logger.debug(f"Dispatcher fast-path: {fast.value}")
            return fast

        # UI conversations should optimize for responsiveness. If the message
        # is still ambiguous here, route it through the normal engine flow
        # instead of paying an extra LLM round-trip for classification.
        if not self.engine.llm:
            return Intent.TASK_REQUEST
        if len(content.strip()) > 60:
            return Intent.TASK_REQUEST

        if not self.engine.llm:
            return Intent.TASK_REQUEST

        try:
            import asyncio as _aio
            raw = await _aio.wait_for(
                self.engine.llm.simple_chat(
                    prompt=content,
                    system=_CLASSIFY_SYSTEM,
                    task_type="quick_tasks",
                ),
                timeout=5,
            )
            parsed = self._parse_json(raw)
            intent_str = parsed.get("intent", "TASK_REQUEST").upper()
            return Intent(intent_str.lower()) if intent_str.lower() in Intent._value2member_map_ else Intent.TASK_REQUEST
        except Exception as e:
            logger.debug(f"Dispatcher classify error/timeout: {e}")
            return Intent.TASK_REQUEST

    # ── Status query ─────────────────────────────────────────────────────

    async def _answer_status_query(self, content: str, task_id: str) -> str:
        """Answer a status-related question by querying the database."""
        if not self.engine.store:
            return "Store is not available."

        # Get the current task
        task = await self.engine.store.get_task(task_id)
        if not task:
            return "I couldn't find the current task."

        # Get all tasks for summary
        project_id = self.engine.project_id or "default"
        all_tasks = await self.engine.store.get_tasks(project_id=project_id)

        status_counts: dict[str, int] = {}
        for t in all_tasks:
            s = t.status.value if hasattr(t.status, "value") else str(t.status)
            status_counts[s] = status_counts.get(s, 0) + 1

        summary_parts = [f"**Current task**: {task.title} — status: {task.status.value}"]
        if len(all_tasks) > 1:
            counts_str = ", ".join(f"{k}: {v}" for k, v in sorted(status_counts.items()))
            summary_parts.append(f"**All tasks** ({len(all_tasks)} total): {counts_str}")

        # Use LLM to produce a natural-language answer from the data
        if self.engine.llm:
            context = "\n".join(summary_parts)
            try:
                answer = await self.engine.llm.simple_chat(
                    prompt=f"User asks: {content}\n\nAvailable data:\n{context}\n\nAnswer concisely.",
                    system="You are a helpful assistant. Answer the user's status question based on the provided data. Be concise.",
                    task_type="quick_tasks",
                )
                return answer.strip()
            except Exception:
                pass

        return "\n".join(summary_parts)

    # ── Session control ──────────────────────────────────────────────────

    async def _handle_session_control(self, content: str, task_id: str) -> str:
        """Handle session-level control commands."""
        lower = content.lower().strip()

        if any(kw in lower for kw in ("cancel", "stop", "abort")):
            if self.engine.store:
                from opc.core.models import TaskStatus
                task = await self.engine.store.get_task(task_id)
                if task and task.status.value not in ("done", "cancelled"):
                    try:
                        await apply_task_status_transition(
                            self.engine.store,
                            task,
                            target_status_or_phase=TaskStatus.CANCELLED,
                            reason="session_control_cancel",
                            release_claim=True,
                        )
                    except Exception:
                        logger.opt(exception=True).warning(
                            f"Could not cancel company runtime task without linked WorkItem: {task_id}"
                        )
                        return "Could not cancel — company runtime task is missing its Work Item link."
                    return f"Task \"{task.title}\" has been cancelled."
            return "Could not cancel — task not found or already completed."

        return "I can help with: cancel/stop task. For other changes, use the kanban board."

    # ── Lightweight conversation ─────────────────────────────────────────

    async def _lightweight_converse(self, content: str, session_id: str | None) -> str:
        """Lightweight LLM conversation with session context (no Task creation)."""
        if not self.engine.llm:
            return "LLM is not available for conversation."

        # Build minimal context from session history
        history_context = ""
        if self.engine.memory and session_id:
            try:
                maybe_compact = getattr(self.engine.memory, "maybe_compact_session_history", None)
                if callable(maybe_compact):
                    await maybe_compact(session_id)
                history_context = await self.engine.memory.build_session_prompt_context(
                    session_id,
                    include_latest_user_turn=False,
                )
            except Exception:
                pass

        system_prompt = (
            "You are OPC, an AI assistant. The user is having a conversation with you. "
            "Answer helpfully and concisely. You do NOT have tool access in this mode — "
            "if the user needs something done (coding, file operations, etc.), "
            "tell them to describe the task and you'll execute it."
        )
        prompt = content
        if history_context:
            prompt = f"Session context:\n{history_context}\n\nUser: {content}"

        try:
            answer = await self.engine.llm.simple_chat(
                prompt=prompt,
                system=system_prompt,
                task_type="quick_tasks",
            )
            return answer.strip() or "I'm not sure how to respond to that."
        except Exception as e:
            logger.error(f"Dispatcher conversation error: {e}")
            return f"Sorry, I encountered an error: {e}"

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n", 1)
            text = lines[1] if len(lines) == 2 else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

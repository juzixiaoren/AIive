"""ReAct Agent Loop: LLM thinks → tool call → result → LLM thinks → respond."""

import json as _json
import re as _re
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from aiive.core.context_builder import ContextBuilder, ContextItem
from aiive.core.llm_client import LLMClient, LLMResponse
from aiive.db.models import ContextSnapshot, Event, Task
from aiive.memory.memory_store import MemoryStore
from aiive.runtime.event_logger import EventLogger
from aiive.runtime.thread_state import ThreadState
from aiive.runtime.trace import Trace
from aiive.tools.registry import get_tool_registry
from aiive.worker.outbox_handlers import register_all
from aiive.worker.outbox_worker import OutboxWorker

MAX_REACT_STEPS = 20


def _serialize_context_item(item: ContextItem) -> dict:
    return {
        "item_id": item.item_id,
        "kind": item.kind,
        "source": item.source,
        "trust_level": item.trust_level,
        "content_preview": item.content_preview,
        "token_estimate": item.token_estimate,
    }


class AgentLoop:
    def __init__(self, llm_client: LLMClient, db: Session):
        self._llm_client = llm_client
        self._db = db
        self._logger = EventLogger(db)
        self._thread_state = ThreadState(db)
        self._ctx_builder = ContextBuilder()
        self._memory_store = MemoryStore(db)
        self._outbox = OutboxWorker(lambda: db)
        register_all(self._outbox)
        self._last_ctx_items: list[ContextItem] = []
        self._last_ctx_meta: dict = {}
        self._last_intent: dict = {}

    def run(self, message: str, thread_id: Optional[str] = None) -> dict:
        trace = Trace.new()
        thread = self._thread_state.get_or_create_thread(thread_id)
        action_cards: list[dict] = []

        if not thread_id:
            self._logger.log_event(trace_id=trace.trace_id, thread_id=thread.id, event_type="chat_started")
        self._logger.log_event(trace_id=trace.trace_id, thread_id=thread.id, event_type="user_message", payload={"content": message})

        # ══ ReAct Loop ══
        tool_results_context = []
        current_messages = self._build_messages(message, thread, trace.trace_id)

        for step in range(MAX_REACT_STEPS):
            response: LLMResponse = self._llm_client.chat(current_messages, trace_id=trace.trace_id)
            reply = response.content

            # Parse tool calls
            tool_matches = _re.findall(r'<tool_call>(.*?)</tool_call>', reply, _re.DOTALL)
            if not tool_matches:
                # No more tool calls → final response
                clean_reply = _re.sub(r'<tool_call>.*?</tool_call>', '', reply, flags=_re.DOTALL).strip()
                return self._finalize(clean_reply or reply, message, thread, trace, action_cards)

            # Execute tools and feed results back
            for tool_json in tool_matches:
                try:
                    tool = _json.loads(tool_json.strip())
                    from aiive.tools.builtin_tools import set_thread_context
                    set_thread_context(thread.id)
                    registry = get_tool_registry()
                    result = registry.execute(tool["name"], tool.get("params", {}), "trusted_user_command")
                    result_text = _json.dumps(result, ensure_ascii=False)
                    tool_results_context.append(f"Tool {tool['name']} result: {result_text}")
                    action_cards.append({
                        "card_type": "tool_result", "title": f"执行: {tool['name']}",
                        "summary": str(tool.get("params", {}))[:80],
                        "trace_id": response.trace_id, "status": "completed" if result.get("ok") else "failed",
                    })
                except Exception:
                    tool_results_context.append(f"Tool call failed: {tool_json[:100]}")

            # Re-build messages with tool results for next LLM round
            current_messages = self._build_messages(message, thread, trace.trace_id)
            if tool_results_context:
                current_messages.append({"role": "system", "content": "Tool execution results:\n" + "\n".join(tool_results_context)})
                current_messages.append({"role": "user", "content": "Continue. Provide final response based on the tool results above."})
                tool_results_context = []

        # Max steps reached → return last response
        return self._finalize(reply, message, thread, trace, action_cards)

    def run_stream(self, message: str, thread_id: Optional[str] = None):
        """Streaming version of run(). Yields SSE-event dicts."""
        trace = Trace.new()
        thread = self._thread_state.get_or_create_thread(thread_id)
        action_cards: list[dict] = []

        if not thread_id:
            self._logger.log_event(trace_id=trace.trace_id, thread_id=thread.id, event_type="chat_started")
        self._logger.log_event(trace_id=trace.trace_id, thread_id=thread.id, event_type="user_message", payload={"content": message})

        current_messages = self._build_messages(message, thread, trace.trace_id)

        for _step in range(MAX_REACT_STEPS):
            # Stream LLM response
            accumulated = ""
            for token in self._llm_client.chat_stream(
                current_messages,
                trace_id=trace.trace_id,
            ):
                accumulated += token
                yield {"event": "token", "data": {"text": token}}

            # Parse tool calls from accumulated text
            reply = accumulated
            tool_matches = _re.findall(r'<tool_call>(.*?)</tool_call>', reply, _re.DOTALL)
            if not tool_matches:
                clean_reply = _re.sub(r'<tool_call>.*?</tool_call>', '', reply, flags=_re.DOTALL).strip()
                result = self._finalize(clean_reply or reply, message, thread, trace, action_cards)
                yield {"event": "done", "data": {
                    "reply": clean_reply or reply,
                    "thread_id": result["thread_id"],
                    "trace_id": result["trace_id"],
                    "action_cards": action_cards,
                }}
                return

            # Execute tools
            for tool_json in tool_matches:
                try:
                    tool = _json.loads(tool_json.strip())
                    from aiive.tools.builtin_tools import set_thread_context
                    set_thread_context(thread.id)
                    registry = get_tool_registry()
                    tool_result = registry.execute(tool["name"], tool.get("params", {}), "trusted_user_command")
                    action_cards.append({
                        "card_type": "tool_result", "title": f"执行: {tool['name']}",
                        "summary": str(tool.get("params", {}))[:80],
                        "trace_id": trace.trace_id,
                        "status": "completed" if tool_result.get("ok") else "failed",
                    })
                    yield {"event": "tool_call", "data": {"name": tool["name"], "params": tool.get("params", {})}}
                    yield {"event": "tool_result", "data": {"name": tool["name"], "result": tool_result}}
                    yield {"event": "action_card", "data": action_cards[-1]}
                except Exception as e:
                    yield {"event": "tool_error", "data": {"raw": tool_json[:100], "error": str(e)}}

            # Re-build messages for next round
            current_messages = self._build_messages(message, thread, trace.trace_id)
            results_text = []
            for idx, tj in enumerate(tool_matches):
                try:
                    t = _json.loads(tj.strip())
                    registry = get_tool_registry()
                    r = registry.execute(t["name"], t.get("params", {}), "trusted_user_command")
                    results_text.append(f"Tool {t['name']} result: {_json.dumps(r, ensure_ascii=False)}")
                except Exception:
                    pass
            if results_text:
                current_messages.append({"role": "system", "content": "Tool execution results:\n" + "\n".join(results_text)})
                current_messages.append({"role": "user", "content": "Continue. Provide final response based on the tool results above."})

        # Max steps reached
        result = self._finalize(reply, message, thread, trace, action_cards)
        yield {"event": "done", "data": {
            "reply": reply,
            "thread_id": result["thread_id"],
            "trace_id": result["trace_id"],
            "action_cards": action_cards,
        }}

    # ------------------------------------------------------------------
    # Context gathering helpers
    # ------------------------------------------------------------------

    def _resolve_memories_for_context(self) -> tuple[list[dict], list[dict]]:
        """Resolve active memories, separating resolved from excluded."""
        all_active = list(self._memory_store.resolve_for_context())
        resolved: list[dict] = []
        excluded: list[dict] = []

        # Group by memory_key; keep only the latest revision for each key
        seen_keys: dict[str, dict] = {}
        for mem in all_active:
            key = mem.memory_key
            if key and key in seen_keys:
                # Older record with same key → exclude (superseded)
                older = seen_keys[key]
                excluded.append({
                    "id": older.id, "content": older.content,
                    "memory_type": older.memory_type, "memory_key": key,
                    "exclusion_reason": "superseded",
                })
                seen_keys[key] = {
                    "id": mem.id, "content": mem.content,
                    "memory_type": mem.memory_type, "memory_key": key,
                }
            elif key:
                seen_keys[key] = {
                    "id": mem.id, "content": mem.content,
                    "memory_type": mem.memory_type, "memory_key": key,
                }
            else:
                resolved.append({"id": mem.id, "content": mem.content, "memory_type": mem.memory_type})

        resolved.extend(seen_keys.values())
        return resolved, excluded

    def _get_runtime_identity(self) -> dict[str, str]:
        """Extract runtime identity from memories with known keys."""
        identity: dict[str, str] = {}
        for mem in self._memory_store.get_active():
            if mem.memory_key == "agent.display_name":
                identity["agent_display_name"] = mem.content
            elif mem.memory_key == "agent.runtime_id":
                identity["agent_runtime_id"] = mem.content
            elif mem.memory_key == "user.name":
                identity["user_display_name"] = mem.content
        return identity

    def _get_tasks_context(self) -> tuple[list[dict], list[dict]]:
        """Return (active_tasks, due_tasks) for context injection."""
        now = datetime.now(timezone.utc)
        all_tasks = (
            self._db.query(Task)
            .filter(Task.status.in_(["pending", "triggered"]))
            .order_by(Task.next_check_at.asc().nullslast())
            .all()
        )
        active: list[dict] = []
        due: list[dict] = []
        for t in all_tasks:
            d = {"id": t.id, "task_type": t.task_type, "title": t.title, "status": t.status}
            if t.next_check_at and t.next_check_at <= now:
                due.append(d)
            else:
                active.append(d)
        return active, due

    def _get_recent_notifications(self) -> list[dict]:
        """Return recent notification events."""
        notifs = (
            self._db.query(Event)
            .filter(Event.event_type == "notification_created")
            .order_by(Event.created_at.desc())
            .limit(5)
            .all()
        )
        return [
            {"id": e.id, "title": e.payload.get("title", ""), "message": e.payload.get("message", "")}
            for e in notifs
        ]

    def _get_tool_schemas_for_context(self) -> dict[str, Any]:
        """Return tool schemas dict with text and injected names."""
        registry = get_tool_registry()
        all_tools = registry.list_all()
        return {
            "tool_schemas_text": registry.render_tool_schemas(),
            "injected_tool_names": [t["capability_id"] for t in all_tools],
        }

    def _infer_intent(self, message: str) -> dict[str, Any]:
        """Use V20 IntentClassifier for structured intent detection."""
        from aiive.core.intent_classifier import IntentClassifier
        classifier = IntentClassifier()
        result = classifier.classify(message, source="trusted_user_message")
        return {
            "intent_type": result.intent_type,
            "execution_mode": result.execution_mode,
            "should_execute": result.should_execute,
            "candidate_tool": result.candidate_tool,
            "memory_type_hint": result.memory_type_hint,
            "memory_key_hint": result.memory_key_hint,
            "reason": result.reason,
        }

    # ------------------------------------------------------------------
    # Intent helpers
    # ------------------------------------------------------------------

    # Patterns that define a high-confidence explain / hypothetical question.
    # These must match the ENTIRE intent of the sentence, not just a substring.
    _EXPLAIN_HYPOTHETICAL_PATTERNS: list[str] = [
        r"如果.*你会怎么做",
        r"假如.*你会怎么做",
        r"你会调用哪个工具",
        r"你会怎么.*(记住|实现|处理|做)",
        r"介绍一下(?!.*帮我)",       # "介绍一下X" but not "介绍一下帮我..."
    ]

    # Explain keywords that ONLY trigger when paired with a question form
    # and NO execution verb appears nearby.
    _EXPLAIN_KEYWORDS: list[str] = [
        "怎么实现", "如何实现", "怎么做",
        "是什么", "什么意思", "解释一下",
    ]

    # Verbs that indicate the user is asking the agent to DO something.
    # If ANY of these appear alongside "能不能", it's a polite REQUEST, not a question.
    _POLITE_REQUEST_ACTION_VERBS: list[str] = [
        "帮我", "给我", "记住", "提醒", "删除", "清空", "清除",
        "忘掉", "忘记", "重置", "创建", "安装", "改名", "设置",
    ]

    def _is_explain_question(self, msg_lower: str, message: str) -> bool:
        """Return True only for high-confidence hypothetical / process questions."""

        # 1. Hypothetical: "如果...你会怎么做" → unconditionally explain_only
        for pat in self._EXPLAIN_HYPOTHETICAL_PATTERNS:
            if _re.search(pat, message):
                return True

        # 2. "能不能告诉我/介绍/解释..." + no action verb → asking for info
        if _re.search(r"能不能(告诉|介绍|解释|说明)", message):
            # If there's also an action verb, it might be a polite request
            has_action = any(v in msg_lower for v in self._POLITE_REQUEST_ACTION_VERBS)
            if not has_action:
                return True

        # 3. "能不能" without any action verb → likely a question
        if "能不能" in msg_lower:
            has_action = any(v in msg_lower for v in self._POLITE_REQUEST_ACTION_VERBS)
            if not has_action:
                return True

        # 4. Standalone explain keywords
        #    "提醒功能怎么实现" → explain_only (怎么实现 dominates)
        #    "帮我解释..." → explain_only (无 action verb)
        for kw in self._EXPLAIN_KEYWORDS:
            if kw in msg_lower:
                # If "能不能帮我X怎么实现" already handled by polite request above
                has_polite_action = any(v in msg_lower for v in self._POLITE_REQUEST_ACTION_VERBS)
                if not has_polite_action or not _re.search(r"能不能(帮|给)", message):
                    return True

        return False

    # Patterns for clear / forget commands
    _CLEAR_FORGET_PATTERNS: list[str] = [
        "清空记忆", "清空", "清除记忆", "清除",
        "忘掉", "忘记", "遗忘",
        "重置记忆", "重置",
        "forget", "clear memory",
        "删除记忆", "清理记忆",
    ]

    # High-confidence execute patterns mapped to intent_type
    _EXECUTE_COMMAND_MAP: list[tuple[list[str], str]] = [
        # Forget / clear → memory_forget_request
        (["清空记忆", "清除记忆", "忘掉", "忘记", "遗忘",
          "重置记忆", "forget", "clear memory", "删除记忆", "清理记忆",
          "清空", "清除", "重置"],
         "memory_forget_request"),
        # Memory write → memory_write_request
        (["记住", "我叫", "以后叫我", "我的名字是", "我的偏好"],
         "memory_write_request"),
        # Reminder → reminder_create
        (["提醒", "定时", "一分钟后"],
         "reminder_create"),
        # Generic execute
        (["马上", "立即", "帮我", "给我", "删除", "创建", "安装"],
         "command"),
    ]

    def _detect_execute_command(self, msg_lower: str, message: str) -> dict[str, Any] | None:
        """Return execute intent dict for high-confidence commands, or None."""

        # 0. Short-circuit: if the message contains explain keywords AND does NOT
        #    have a direct action structure, it's not a clear execute command.
        has_explain_kw = any(kw in msg_lower for kw in self._EXPLAIN_KEYWORDS)
        has_polite = "能不能" in msg_lower
        has_action_verb = any(v in msg_lower for v in self._POLITE_REQUEST_ACTION_VERBS)
        is_polite_request = has_polite and has_action_verb

        if has_explain_kw and not is_polite_request:
            return None

        # 1. "能不能帮我X" → execute (polite request)
        if is_polite_request:
            intent_type = "command"
            for pat in self._CLEAR_FORGET_PATTERNS:
                if pat in msg_lower and len(pat) >= 2:
                    intent_type = "memory_forget_request"
                    break
            return {
                "intent_type": intent_type,
                "execution_mode": "execute",
                "should_execute": True,
            }

        # 2. High-confidence execution patterns
        for patterns, intent_type in self._EXECUTE_COMMAND_MAP:
            for pat in patterns:
                if pat in msg_lower:
                    return {
                        "intent_type": intent_type,
                        "execution_mode": "execute",
                        "should_execute": True,
                    }

        return None

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def _build_messages(self, message: str, thread, trace_id: str = "") -> list[dict]:
        # Memory
        resolved_memories, excluded_memories = self._resolve_memories_for_context()

        # Identity
        runtime_identity = self._get_runtime_identity()

        # Intent
        intent_result = self._infer_intent(message)
        self._last_intent = intent_result

        # Tasks
        active_tasks, due_tasks = self._get_tasks_context()

        # Notifications
        recent_notifications = self._get_recent_notifications()

        # Tool schemas
        active_tool_schemas = self._get_tool_schemas_for_context()

        # History
        history = self._thread_state.get_recent_messages(thread.id)

        messages, items, meta = self._ctx_builder.build(
            history=history,
            current_message=message,
            thread_id=thread.id,
            trace_id=trace_id,
            runtime_identity=runtime_identity,
            intent_result=intent_result,
            resolved_memories=resolved_memories,
            excluded_memories=excluded_memories,
            active_tasks=active_tasks,
            due_tasks=due_tasks,
            recent_notifications=recent_notifications,
            active_tool_schemas=active_tool_schemas,
        )
        self._last_ctx_items = items
        self._last_ctx_meta = meta
        return messages

    def _finalize(self, reply: str, message: str, thread, trace, action_cards: list[dict]) -> dict:
        self._logger.log_event(trace_id=trace.trace_id, thread_id=thread.id, event_type="llm_response", payload={"content": reply})

        self._outbox.enqueue(db=self._db, job_type="memory_extraction", payload={"user_message": message, "reply": reply, "thread_id": thread.id, "intent_type": self._last_intent.get("intent_type", "chat"), "execution_mode": self._last_intent.get("execution_mode", "explain_only"), "should_execute": self._last_intent.get("should_execute", False)}, trace_id=trace.trace_id)
        self._outbox.enqueue(db=self._db, job_type="steward_extraction", payload={"user_message": message, "reply": reply, "thread_id": thread.id, "intent_type": self._last_intent.get("intent_type", "chat"), "execution_mode": self._last_intent.get("execution_mode", "explain_only"), "should_execute": self._last_intent.get("should_execute", False)}, trace_id=trace.trace_id)

        meta = dict(self._last_ctx_meta) if self._last_ctx_meta else {}
        snapshot = ContextSnapshot(
            trace_id=trace.trace_id,
            thread_id=thread.id,
            stable_prefix_hash=self._last_ctx_meta.get("stable_prefix_hash", ""),
            context_items=[_serialize_context_item(it) for it in self._last_ctx_items],
            meta={
                "total_items": meta.get("total_items", 0),
                "total_tokens": meta.get("total_tokens", 0),
                "thread_id": meta.get("thread_id", ""),
                "trace_id": meta.get("trace_id", ""),
                "injected_memory_ids": meta.get("injected_memory_ids", []),
                "excluded_memory_ids": meta.get("excluded_memory_ids", []),
                "exclusion_reasons": meta.get("exclusion_reasons", []),
                "injected_tool_names": meta.get("injected_tool_names", []),
                "injected_policy_ids": meta.get("injected_policy_ids", []),
                "active_task_ids": meta.get("active_task_ids", []),
                "due_task_ids": meta.get("due_task_ids", []),
                "notification_ids": meta.get("notification_ids", []),
                "truncated": meta.get("truncated", False),
                "truncated_message_ids": meta.get("truncated_message_ids", []),
                "budget_chars": meta.get("budget_chars", 0),
                "used_chars": meta.get("used_chars", 0),
            },
        )
        self._db.add(snapshot)
        self._outbox.process_all(self._db, max_jobs=10)
        from aiive.worker.task_worker import TaskWorker
        TaskWorker(self._db).poll_and_notify()
        self._db.commit()

        return {"reply": reply, "thread_id": thread.id, "trace_id": trace.trace_id, "action_cards": action_cards, "intent_type": meta.get("intent_type", "plain_chat")}

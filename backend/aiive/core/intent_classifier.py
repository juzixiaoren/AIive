"""V20 Intent Classifier: structured intent detection.

Replaces the keyword-matching _infer_intent() in agent_loop.
Outputs IntentResult with intent_type, execution_mode, should_execute, source tracking.
"""

from dataclasses import dataclass, field
from typing import Optional

from aiive.config import settings
from aiive.core.llm_client import LLMClient


# ---------------------------------------------------------------------------
# Structured output
# ---------------------------------------------------------------------------

@dataclass
class IntentResult:
    intent_type: str
    execution_mode: str  # "explain_only" | "dry_run" | "execute"
    should_execute: bool
    source: str = "trusted_user_message"
    candidate_tool: str | None = None
    candidate_action: str | None = None
    reason: str = ""
    memory_type_hint: str | None = None
    memory_key_hint: str | None = None

    def to_dict(self) -> dict:
        return {
            "intent_type": self.intent_type,
            "execution_mode": self.execution_mode,
            "should_execute": self.should_execute,
            "source": self.source,
            "candidate_tool": self.candidate_tool,
            "candidate_action": self.candidate_action,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Allowed intent types for MemoryGate (memory-related only)
# ---------------------------------------------------------------------------

MEMORY_ELIGIBLE_INTENT_TYPES = frozenset({
    "memory_update",
    "user_identity_update",
    "agent_identity_update",
    "user_preference_update",
    "agent_persona_update",
    "project_decision_update",
    "policy_memory_update",
})

# Intent types that should be routed to Task/Scheduler, NOT Memory
SCHEDULER_INTENT_TYPES = frozenset({
    "reminder_create",
    "routine_create",
    "scheduled_tool_task",
})


# ---------------------------------------------------------------------------
# Rule-based patterns (deterministic, fast path)
# ---------------------------------------------------------------------------

def _classify_rules(message: str) -> IntentResult | None:
    """Fast rule-based detection. Returns None if ambiguous (→ model_decide).

    Rules ordered by confidence: explain/hypothetical first, then execute commands,
    then explain keywords. Ambiguous messages return None.
    """
    msg = message.strip()
    msg_lower = msg.lower()

    # ------------------------------------------------------------------
    # HIGH CONFIDENCE: Hypothetical / explain questions (unconditionally)
    # ------------------------------------------------------------------

    # "如果...你会怎么做" ↔ pure hypothetical
    if ("调用哪个工具" in msg or "用哪个工具" in msg or "what tool" in msg_lower or
        ("如果" in msg and "怎么做" in msg) or ("如果" in msg and "你会" in msg)):
        return IntentResult(
            intent_type="tool_choice_question",
            execution_mode="explain_only",
            should_execute=False,
            reason="User asking about tool choice, not requesting execution",
        )

    if ("如果我想" in msg or "假设" in msg or "if i wanted" in msg_lower):
        if not any(kw in msg_lower for kw in ("现在", "立即", "马上", "now", "do it")):
            return IntentResult(
                intent_type="hypothetical_question",
                execution_mode="explain_only",
                should_execute=False,
                reason="Hypothetical scenario, not a command",
            )

    # "能不能告诉/介绍/解释/说明..." → asking for info, not execution request
    if any(kw in msg for kw in ("能不能告诉", "能不能介绍", "能不能解释", "能不能说明")):
        return IntentResult(
            intent_type="normal_chat",
            execution_mode="explain_only",
            should_execute=False,
            reason="User asking for explanation, not execution",
        )

    # ------------------------------------------------------------------
    # HIGH CONFIDENCE: Execute commands
    # Checked BEFORE explain patterns so execution intent takes priority.
    # ------------------------------------------------------------------

    # --- memory_forget_request (clear/forget) ---
    _CLEAR_PATTERNS = (
        "清空记忆", "清除记忆", "清空", "清除", "清理记忆",
        "忘掉", "忘记", "遗忘",
        "重置记忆", "重置",
        "forget", "clear memory",
        "删除记忆",
    )
    for pat in _CLEAR_PATTERNS:
        if pat in msg_lower:
            return IntentResult(
                intent_type="memory_forget_request",
                execution_mode="execute",
                should_execute=True,
                candidate_tool="forget_memory",
                reason="User requesting memory deletion or forget",
            )

    # --- agent_identity_update ---
    if ("以后你叫" in msg or "你改名叫" in msg or "你的名字是" in msg or
        "从现在起你叫" in msg or "your name is" in msg_lower):
        return IntentResult(
            intent_type="agent_identity_update",
            execution_mode="execute",
            should_execute=True,
            candidate_tool="remember_or_update",
            memory_type_hint="agent_self",
            memory_key_hint="agent.display_name",
            reason="User explicitly changing agent display name",
        )

    # --- user_identity_update (display name) ---
    if ("以后叫我" in msg or "称呼我" in msg or "叫我" in msg or "call me" in msg_lower):
        return IntentResult(
            intent_type="user_identity_update",
            execution_mode="execute",
            should_execute=True,
            candidate_tool="remember_or_update",
            memory_type_hint="user_profile",
            memory_key_hint="user.display_name",
            reason="User changing preferred display name",
        )

    # --- user real name ---
    if ("我的真实姓名" in msg or "我真名叫" in msg or "my real name" in msg_lower):
        return IntentResult(
            intent_type="user_identity_update",
            execution_mode="execute",
            should_execute=True,
            candidate_tool="remember_or_update",
            memory_type_hint="user_profile",
            memory_key_hint="user.name",
            reason="User providing real name",
        )

    # --- reminder_create (must NOT be an explain question) ---
    if (("提醒" in msg and ("分钟" in msg or "小时后" in msg or "秒后" in msg)) or
        ("一分钟后" in msg) or ("remind" in msg_lower and "minute" in msg_lower)):
        if not ("怎么实现" in msg or "如何实现" in msg or "怎么做" in msg):
            return IntentResult(
                intent_type="reminder_create",
                execution_mode="execute",
                should_execute=True,
                candidate_tool="schedule_reminder",
                reason="One-time reminder request",
            )

    # --- routine_create ---
    if (("每天" in msg and ("提醒" in msg or "通知" in msg)) or
        ("每周" in msg and ("提醒" in msg or "通知" in msg)) or
        ("every day" in msg_lower and "remind" in msg_lower)):
        return IntentResult(
            intent_type="routine_create",
            execution_mode="execute",
            should_execute=True,
            candidate_tool="schedule_reminder",
            reason="Recurring routine request, route to Scheduler",
        )

    # --- user_preference_update (interaction style) ---
    if (("我喜欢" in msg or "我不喜欢" in msg or "我偏好" in msg or "我讨厌" in msg) and
        "你" in msg and ("回答" in msg or "说话" in msg or "回复" in msg or "表现" in msg or
                         "respond" in msg_lower or "reply" in msg_lower)):
        return IntentResult(
            intent_type="user_preference_update",
            execution_mode="execute",
            should_execute=True,
            candidate_tool="remember_or_update",
            memory_type_hint="preference",
            memory_key_hint="user.preference.response_style",
            reason="User expressing interaction preference",
        )

    # --- memory_update (explicit remember command) ---
    if (("记住" in msg or "别忘了" in msg or "记下" in msg or "don't forget" in msg_lower) and
        not ("怎么" in msg or "如果" in msg)):
        return IntentResult(
            intent_type="memory_update",
            execution_mode="execute",
            should_execute=True,
            candidate_tool="remember_or_update",
            reason="Explicit memory command",
        )

    # --- polite request: "能不能帮我X" → execute ---
    _POLITE_ACTION_VERBS = (
        "帮我", "给我", "记住", "提醒", "删除", "清空", "清除",
        "忘掉", "忘记", "重置", "创建", "安装", "改名", "设置",
    )
    if "能不能" in msg_lower and any(v in msg_lower for v in _POLITE_ACTION_VERBS):
        return IntentResult(
            intent_type="command",
            execution_mode="execute",
            should_execute=True,
            reason="Polite execution request",
        )

    # --- user identity (我叫...) ---
    if "我叫" in msg:
        return IntentResult(
            intent_type="user_identity_update",
            execution_mode="execute",
            should_execute=True,
            candidate_tool="remember_or_update",
            memory_type_hint="user_profile",
            memory_key_hint="user.name",
            reason="User providing their name",
        )

    # --- explicit execute triggers (including delete) ---
    execute_triggers = ("现在", "立即", "马上", "帮我", "给我", "删除", "now", "do it")
    if any(t in msg_lower for t in execute_triggers):
        return IntentResult(
            intent_type="command",
            execution_mode="execute",
            should_execute=True,
            reason="Explicit execute directive",
        )

    # ------------------------------------------------------------------
    # HIGH CONFIDENCE: Explain / info-seeking (check AFTER execute)
    # ------------------------------------------------------------------

    # "怎么实现" / "如何实现" → clearly asking about implementation
    if "怎么实现" in msg or "如何实现" in msg:
        return IntentResult(
            intent_type="normal_chat",
            execution_mode="explain_only",
            should_execute=False,
            reason="User asking about implementation details",
        )

    # "是什么意思" / "什么意思" → asking for definition
    if "是什么意思" in msg or "什么意思" in msg:
        return IntentResult(
            intent_type="normal_chat",
            execution_mode="explain_only",
            should_execute=False,
            reason="User asking for definition",
        )

    # ------------------------------------------------------------------
    # Ambiguous → return None (model_decide in classify() default)
    # Removed: old msg.endswith("?") rule (too broad, misclassified
    #   "清空记忆？" as explain_only).
    # ------------------------------------------------------------------
    return None


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

class IntentClassifier:
    def __init__(self, llm_client: LLMClient | None = None):
        self._llm = llm_client

    def classify(self, message: str, source: str = "trusted_user_message") -> IntentResult:
        # Step 1: Fast rule path
        result = _classify_rules(message)
        if result is not None:
            result.source = source
            return result

        # Step 2: LLM fallback (for ambiguous cases)
        if self._llm is not None:
            result = self._classify_llm(message, source)
            if result is not None:
                return result

        # Step 3: model_decide — let LLM/Dispatcher make the final call
        return IntentResult(
            intent_type="normal_chat",
            execution_mode="model_decide",
            should_execute=False,
            source=source,
            reason="Ambiguous: let model decide based on full context",
        )

    def _classify_llm(self, message: str, source: str) -> IntentResult | None:
        """LLM-based classification for ambiguous messages."""
        prompt = (
            "Classify the user message into one intent type. Respond with JSON only.\n\n"
            "Intent types:\n"
            "- tool_choice_question: asking which tool would be used\n"
            "- hypothetical_question: \"if I wanted to...\" scenarios\n"
            "- agent_identity_update: changing agent's name/persona\n"
            "- user_identity_update: changing user's name/preferred name\n"
            "- memory_update: persisting a fact/preference\n"
            "- user_preference_update: changing interaction style preference\n"
            "- reminder_create: one-time timed reminder\n"
            "- routine_create: recurring task/routine\n"
            "- preference_statement: \"I like/prefer...\" (not an update to remembered preferences)\n"
            "- normal_chat: casual conversation, questions, debugging\n\n"
            "Output JSON: {\"intent_type\": \"...\", \"execution_mode\": \"explain_only|dry_run|execute\", "
            "\"should_execute\": true|false, \"candidate_tool\": null|\"...\", \"reason\": \"...\"}\n\n"
            f"User message: {message}\n\n"
            "Output ONLY valid JSON:"
        )
        try:
            response = self._llm.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.1,
            )
            import json as _json
            data = _json.loads(response.content.strip())
            return IntentResult(
                intent_type=data.get("intent_type", "normal_chat"),
                execution_mode=data.get("execution_mode", "explain_only"),
                should_execute=data.get("should_execute", False),
                source=source,
                candidate_tool=data.get("candidate_tool"),
                reason=data.get("reason", "LLM classification"),
            )
        except Exception:
            return None

"""V20 MemoryGate: structured memory admission policy.

TRANSFORMED: Removed ALL keyword tables. MemoryGate now decides purely on structured
fields from IntentResult and ExtractedMemory. Natural language understanding
is the responsibility of IntentClassifier and MemoryExtractor, NOT MemoryGate.
"""

from dataclasses import dataclass, field

# Ordered by priority: single-key means at most one active record with this key
SINGLE_KEY_KEYS = frozenset({
    "user.display_name",
    "user.name",
    "agent.display_name",
    "user.preference.response_style",
})

# Keys that always start with a known prefix
KNOWN_KEY_PREFIXES = frozenset({
    "user.display_name", "user.name", "user.preference.",
    "agent.display_name", "agent.persona.",
    "project.",
})

# Intent types eligible for memory admission (others → reject or delegate to Scheduler)
MEMORY_ELIGIBLE = frozenset({
    "memory_update",
    "user_identity_update",
    "agent_identity_update",
    "user_preference_update",
    "agent_persona_update",
    "project_decision_update",
    "policy_memory_update",
})

# Intent types that must be routed to Scheduler, NOT MemoryGate
SCHEDULER_INTENTS = frozenset({
    "reminder_create",
    "routine_create",
    "scheduled_tool_task",
})

# Memory types that must NOT come from untrusted sources
TRUST_REQUIRED_MEMORY_TYPES = frozenset({
    "user_profile", "agent_self", "preference", "policy",
})


# ---------------------------------------------------------------------------
# 结构化输入/输出数据类
# ---------------------------------------------------------------------------

@dataclass
class MemoryGateInput:
    """记忆准入决策的输入数据结构。

    Attributes:
        content: 记忆内容文本。
        user_message: 用户原始消息。
        source: 来源标识（auto_extraction | tool_call | steward）。
        intent_type: 意图分类结果。
        execution_mode: 执行模式（explain_only | execute）。
        should_execute: 是否允许执行。
        evidence_source: 证据来源（trusted_user_message | untrusted_external_content | tool_result）。
        extracted_memory_type: 从提取器中获得的记忆类型。
        extracted_memory_key: 从提取器中获得的记忆键。
        confidence: 置信度。
        existing_memory: 已存在的同名记忆记录（用于替代决策）。
        trace_id: 追踪 ID。
        source_event_id: 源事件 ID。
    """
    content: str
    user_message: str
    source: str = "auto_extraction"  # 来源：auto_extraction | tool_call | steward
    intent_type: str = "unknown"
    execution_mode: str = "explain_only"
    should_execute: bool = False
    evidence_source: str = "trusted_user_message"  # 证据来源：trusted_user_message | untrusted_external_content | tool_result
    extracted_memory_type: str | None = None
    extracted_memory_key: str | None = None
    confidence: float = 0.5
    existing_memory: dict | None = None
    trace_id: str = ""
    source_event_id: str = ""


@dataclass
class MemoryGateDecision:
    """记忆准入决策的输出数据结构。

    Attributes:
        decision: 决策结果（active 直接写入 | candidate 候选队列 | reject 拒绝）。
        memory_type: 最终确定的记忆类型。
        memory_key: 最终确定的记忆键。
        update_mode: 更新模式（create 新建 | upsert 更新插入 | supersede 替代旧记录 | ignore 忽略）。
        confidence: 最终置信度。
        reason: 决策原因说明。
        blocked_reason: 阻止原因代码。
        supersede_memory_ids: 需要被替代的旧记忆 ID 列表。
        requires_user_confirmation: 是否需要用户确认。
        source_event_id: 源事件 ID。
        trace_id: 追踪 ID。
    """
    decision: str  # "active" | "candidate" | "reject"
    memory_type: str | None = None
    memory_key: str | None = None
    update_mode: str = "create"  # "create" | "upsert" | "supersede" | "ignore"
    confidence: float = 0.5
    reason: str = ""
    blocked_reason: str = ""
    supersede_memory_ids: list[str] = field(default_factory=list)
    requires_user_confirmation: bool = False
    source_event_id: str = ""
    trace_id: str = ""


# ---------------------------------------------------------------------------
# MemoryKeyResolver - 记忆键解析器（确定性逻辑，无 NLP）
# ---------------------------------------------------------------------------

class MemoryKeyResolver:
    """从结构化的 ExtractedMemory 提示中解析出稳定的 memory_key。

    完全基于确定性规则，不涉及任何 NLP 或关键词匹配。
    """

    @staticmethod
    def resolve(memory_type_hint: str | None, memory_key_hint: str | None,
                content: str) -> str | None:
        """根据记忆类型提示和键提示解析出最终的 memory_key。

        Args:
            memory_type_hint: 记忆类型提示。
            memory_key_hint: 记忆键提示（来自 IntentClassifier 的显式提示优先）。
            content: 记忆内容文本。

        Returns:
            解析出的稳定键，无法确定时返回 None。
        """
        # IntentClassifier 的显式提示优先级最高
        if memory_key_hint and memory_key_hint in KNOWN_KEY_PREFIXES:
            return memory_key_hint
        if memory_key_hint:
            return memory_key_hint

        # 回退逻辑：尝试从 memory_type 推导
        if memory_type_hint == "user_profile":
            return "user.display_name"
        if memory_type_hint == "agent_self":
            return "agent.display_name"
        if memory_type_hint == "preference":
            return "user.preference.response_style"

        return None


# ---------------------------------------------------------------------------
# MemoryGate - 记忆准入控制器
# ---------------------------------------------------------------------------

class MemoryGate:
    """记忆准入策略：仅基于结构化输入做决策，不使用 NLP 或关键词。"""

    def __init__(self):
        self._key_resolver = MemoryKeyResolver()

    # pylint: disable=too-many-return-statements,too-many-branches
    def decide(self, inp: MemoryGateInput) -> MemoryGateDecision:
        """基于结构化字段执行纯准入决策。

        依次检查执行模式、权限、信任边界、意图类型、记忆键完整性、
        置信度阈值、已存在记录和单键唯一性等规则。

        Args:
            inp: 记忆准入输入数据。

        Returns:
            MemoryGateDecision 决策结果。
        """

        # ---- 规则 1：执行模式检查 ----
        if inp.execution_mode != "execute":
            return MemoryGateDecision(
                decision="reject",
                reason="execution_mode is not execute",
                blocked_reason="not_execute",
                trace_id=inp.trace_id,
            )

        # ---- 规则 2：执行权限检查 ----
        if not inp.should_execute:
            return MemoryGateDecision(
                decision="reject",
                reason="should_execute is false",
                blocked_reason="should_execute_false",
                trace_id=inp.trace_id,
            )

        # ---- 规则 3：信任边界检查 ----
        # 敏感记忆类型（个人信息、偏好等）必须来自可信用户消息
        if (inp.evidence_source != "trusted_user_message" and
            inp.extracted_memory_type in TRUST_REQUIRED_MEMORY_TYPES):
            return MemoryGateDecision(
                decision="reject",
                reason=f"untrusted source '{inp.evidence_source}' cannot write {inp.extracted_memory_type}",
                blocked_reason="blocked_by_trust_boundary",
                trace_id=inp.trace_id,
            )

        # ---- 规则 4：意图类型准入检查 ----
        if inp.intent_type not in MEMORY_ELIGIBLE:
            if inp.intent_type in SCHEDULER_INTENTS:
                # 调度相关意图应路由到 Scheduler
                return MemoryGateDecision(
                    decision="reject",
                    reason=f"intent '{inp.intent_type}' should be routed to Scheduler, not MemoryGate",
                    blocked_reason="route_to_scheduler",
                    trace_id=inp.trace_id,
                )
            return MemoryGateDecision(
                decision="reject",
                reason=f"intent_type '{inp.intent_type}' not eligible for memory admission",
                blocked_reason="not_memory_intent",
                trace_id=inp.trace_id,
            )

        # ---- 规则 5：记忆键完整性检查 ----
        # 没有稳定键的记忆不能直接激活，先放入候选队列
        memory_key = inp.extracted_memory_key
        if not memory_key or not memory_key.strip():
            return MemoryGateDecision(
                decision="candidate",
                memory_type=inp.extracted_memory_type,
                confidence=inp.confidence,
                reason="no stable memory_key; queued as candidate",
                trace_id=inp.trace_id,
            )

        # ---- 规则 6：置信度阈值检查 ----
        # 置信度低于 0.7 的记忆先放入候选队列等待人工确认
        if inp.confidence < 0.7:
            return MemoryGateDecision(
                decision="candidate",
                memory_type=inp.extracted_memory_type,
                memory_key=memory_key,
                confidence=inp.confidence,
                reason=f"confidence {inp.confidence} below 0.7 threshold",
                trace_id=inp.trace_id,
            )

        # ---- 规则 7：替代已存在记录 ----
        # 如果存在相同键的旧记忆，标记为替代模式
        if inp.existing_memory and inp.existing_memory.get("id"):
            return MemoryGateDecision(
                decision="active",
                memory_type=inp.extracted_memory_type or inp.existing_memory.get("memory_type"),
                memory_key=memory_key,
                update_mode="supersede",
                confidence=max(inp.confidence, 0.9),
                reason="superseding existing memory with same key",
                supersede_memory_ids=[inp.existing_memory["id"]],
                source_event_id=inp.source_event_id,
                trace_id=inp.trace_id,
            )

        # ---- 规则 8：单键唯一性检查 ----
        # 单键记忆标记为 upsert（写入服务会确保只有一条活跃记录）
        if memory_key in SINGLE_KEY_KEYS:
            return MemoryGateDecision(
                decision="active",
                memory_type=inp.extracted_memory_type,
                memory_key=memory_key,
                update_mode="upsert",
                confidence=inp.confidence,
                reason=f"single-key memory '{memory_key}' admitted",
                source_event_id=inp.source_event_id,
                trace_id=inp.trace_id,
            )

        # ---- 规则 9：默认通过 ----
        # 所有检查通过，正常创建活跃记忆
        return MemoryGateDecision(
            decision="active",
            memory_type=inp.extracted_memory_type,
            memory_key=memory_key,
            update_mode="create",
            confidence=inp.confidence,
            reason="all checks passed",
            source_event_id=inp.source_event_id,
            trace_id=inp.trace_id,
        )

    # ------------------------------------------------------------------
    # 向后兼容的包装方法（已弃用——生产环境必须使用 decide()）
    # ------------------------------------------------------------------

    def decide_str(self, content: str, user_message: str) -> str:
        """已弃用：请使用 decide(MemoryGateInput) 代替。

        仅用于向后兼容，返回 'active'、'candidate' 或 'reject'。

        Args:
            content: 记忆内容。
            user_message: 用户消息。

        Returns:
            决策结果字符串。
        """
        result = self.decide(MemoryGateInput(
            content=content, user_message=user_message,
            execution_mode="explain_only", should_execute=False,
        ))
        return result.decision

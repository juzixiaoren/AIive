"""
模块功能说明：
- 轻量级意图提取器，仅用于日志记录和追踪（logging/tracing）
- 在 LangGraph 重构后，工具调用由 LLM + bind_tools() 原生机制处理
- 本模块保留用于 outbox 意图提取（memory_extraction、steward_extraction）和日志记录
- 不做确定性分发逻辑——工具调用由 LLM 通过原生 tool_calls 决定
"""

from typing import Any

from pydantic import BaseModel, Field

from aiive.core.llm_client import LLMClient


class AgentDecision(BaseModel):
    """轻量级决策模型，仅用于日志记录和追踪。

    不再用于工具分发——工具分发现在由 LangGraph 原生 tool_calls + policy_check 节点处理。
    """
    decision_type: str = "final_response"
    execution_mode: str = "explain_only"
    intent_type: str = "normal_chat"
    should_execute: bool = False
    tool_name: str | None = None
    tool_params: dict[str, Any] = Field(default_factory=dict)
    args: dict[str, Any] = Field(default_factory=dict)
    requires_confirmation: bool = False
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    parse_failed: bool = False
    reason: str = ""

    def to_intent_dict(self) -> dict[str, Any]:
        """将决策信息转换为意图字典，供下游日志模块使用。"""
        return {
            "intent_type": self.intent_type,
            "execution_mode": self.execution_mode,
            "should_execute": self.should_execute,
            "candidate_tool": self.tool_name,
            "parse_failed": self.parse_failed,
            "reason": self.reason,
        }


class ActionPlanner:
    """最小化规划器：提供意图提取能力，仅用于日志记录。

    不做工具分发逻辑。主 agent graph（agent_graph.py）通过 LLM + bind_tools() 原生机制处理所有工具调用。
    """

    def __init__(self, llm_client: LLMClient):
        """初始化规划器。

        参数:
            llm_client: LLM 客户端实例，留作后续可能的使用
        """
        self._llm: LLMClient = llm_client

    def plan(
        self,
        user_message: str,
        _runtime_identity: dict[str, str] | None = None,
        tool_schemas_text: str = "",
        trace_id: str | None = None,
    ) -> AgentDecision:
        """提取用户消息的意图信息，仅用于日志记录，不做实际的工具分发。

        参数:
            user_message: 用户的原始消息内容
            runtime_identity: 运行时身份信息字典
            tool_schemas_text: 可用工具 schema 的文本描述
            trace_id: 追踪 ID

        返回值:
            AgentDecision: 包含意图类型和执行模式的决策对象
        """
        # 简单启发式：没有可用工具 schema 时，直接视为普通对话（仅用于日志追踪）
        intent_type = "normal_chat" if not tool_schemas_text else "tool_bound_chat"
        return AgentDecision(
            decision_type="final_response",
            execution_mode="explain_only",
            intent_type=intent_type,
            should_execute=False,
            tool_name=None,
            reason=(
                "Intent extraction deferred to LLM native tool_calls "
                f"(trace_id={trace_id}, user_message={user_message[:50]!r})"
            ),
        )

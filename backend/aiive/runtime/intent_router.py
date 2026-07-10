"""
运行时层 - 意图路由（现由 LLM 驱动）。

本模块仅保留用于追踪和日志目的的数据结构，不再做基于正则的意图路由。
所有意图识别已由 LLM 通过原生 tool_calls 机制接管。
"""

from typing import Any, Literal

from pydantic import BaseModel

# 意图类型：覆盖纯聊天、记忆操作、工具调用、删除、知识管理、自进化等全部场景
IntentType = Literal[
    "plain_chat", "memory_write", "memory_revision",
    "tool_call", "safe_delete", "knowledge_ingest", "knowledge_search",
    "forget_memory", "maintenance_scan", "mcp_search", "mcp_sandbox_install",
    "selfdev_plan", "selfdev_apply_inactive", "selfdev_promote", "selfdev_rollback",
    "rhythm_query", "attention_query", "show_notifications",
]


class IntentResult(BaseModel):
    """意图检测结果数据模型。

    Attributes:
        intent_type: 检测到的意图类型
        confidence: 置信度（0-1）
        instruction_source: 指令来源（受信任用户命令/受信任审批/不受信任内容）
        extracted_args: 提取的参数
        requires_confirmation: 是否需要用户确认
        reason: 检测原因说明
    """
    intent_type: IntentType = "plain_chat"
    confidence: float = 1.0
    instruction_source: Literal["trusted_user_command", "trusted_approval", "untrusted_content"] = "trusted_user_command"
    extracted_args: dict[str, Any] = {}
    requires_confirmation: bool = False
    reason: str = ""


class IntentRouter:
    """意图路由器（适配层）。

    所有意图由 LLM 通过原生 tool_calls 标签确定，本类始终返回 plain_chat，
    仅用于保持 API 兼容性。
    """
    def detect(self, _message: str) -> IntentResult:
        """检测消息意图（适配层实现，始终返回 plain_chat）。"""
        return IntentResult(intent_type="plain_chat", reason="LLM-driven")

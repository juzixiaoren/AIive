"""Main Agent 单 Turn 工具面：唯一注册入口。

工具面 = 固定任务元工具 + 附件工具 + 本轮条件工具：
- notify_user：每个 Turn 都注册（服务端仍按 turn_event_inbox 授权）；
- answer_goal_question：只在确有未关闭问题时注册，避免把闲聊误判成回答。

ContextAssembler 用本入口生成 Schema 并计入 Token 预算，AgentGraph 用同一个
注册表绑定执行，ContextInspector 用本入口离线展示，三者不再各自维护注册时机。
"""
from __future__ import annotations

from typing import Any

from aiive.runtime.proactive_tool import register_notify_user
from aiive.runtime.question_tool import register_answer_goal_question
from aiive.task_runtime.tools import build_main_agent_registry
from aiive.tools.registry import ToolRegistry


def build_turn_tool_registry(open_questions: list[Any] | None = None) -> ToolRegistry:
    """构建本轮对话 Agent 的真实工具面（固定元工具 + 条件工具）。"""
    registry = build_main_agent_registry()
    register_notify_user(registry)
    if open_questions:
        register_answer_goal_question(registry)
    return registry

"""稳定系统提示词构建：供上下文组装与图执行共享，避免反向依赖图编排。"""
from __future__ import annotations

from typing import Any

from aiive.prompts import get_prompt_registry


def build_runtime_identity(runtime_identity: dict[str, str] | None) -> str:
    """渲染 Runtime Identity 块：只放当前身份字段。"""
    if not runtime_identity:
        return ""
    lines = ["## Runtime Identity"]
    rid = runtime_identity.get("agent_runtime_id", "")
    if rid:
        lines.append(f"- agent_runtime_id: {rid}")
    aname = runtime_identity.get("agent_display_name", "")
    if aname:
        lines.append(f"- agent_display_name: {aname}")
    rstyle = runtime_identity.get("relationship_style", "")
    if rstyle:
        lines.append(f"- relationship_style: {rstyle}")
    unset_fields = [key for key in ("user_display_name", "user_name", "response_style")
                    if runtime_identity.get(key) == "[暂未设置]"]
    lines.extend(f"- {key}: [暂未设置]" for key in unset_fields)
    if any(value == "[暂未设置]" for value in runtime_identity.values()):
        lines.append("未设置的身份槽不得从其他 Key、历史摘要或模型推断恢复；只有用户的新设置可以赋值。")
    if len(lines) == 1:
        return ""
    return "\n".join(lines) + "\n"


def build_stable_contract(
    runtime_identity: dict[str, str] | None = None,
    policies: list[dict[str, Any]] | None = None,
) -> str:
    """构建稳定系统契约（Kernel Contract）：基础指令 + 身份 + 策略。

    仅包含不可变/稳定的指令与精确 key 身份、policy，不注入普通长期记忆列表，
    也不每轮注入全部 active/due tasks（V2 §十二）。动态记忆由 Automatic Recall
    在调用链后置注入。
    """
    parts = [get_prompt_registry().render("agent.stable_contract").content]

    identity_text = build_runtime_identity(runtime_identity)
    if identity_text:
        parts.append(identity_text)

    if policies:
        parts.append("\n## Active Policies (user-defined rules — instructions, not memory)")
        for p in policies:
            parts.append(f"- {p.get('content', '')}")

    return "\n".join(parts)

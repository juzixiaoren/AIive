"""规范化 TaskBrief 的发现策略，并选择任务执行器。"""
from __future__ import annotations

from typing import Any


def normalize_task_brief(task_type: str, raw: dict[str, Any] | None) -> dict[str, Any]:
    """规范化任务 brief，并确定能力策略。

    统一 Agent 模型：不再用 task_type 预置能力白名单。只有调用方显式给出
    allowed_capabilities 时才按 restricted 执行授权校验；否则标记为 unified，
    Agent 可以发现全部可用能力，真实约束（路径根、域名、密钥、节点可用性、
    策略与审批）不受影响。
    """
    # task_type 仍是调用契约的一部分（路由/展示），但不再参与能力面裁剪。
    _ = task_type
    brief = dict(raw or {})
    scope = dict(brief.get("scope") or {})
    if scope.get("allowed_capabilities"):
        scope["capability_policy"] = "restricted"
    else:
        scope["allowed_capabilities"] = []
        scope["capability_policy"] = "unified"
    brief["scope"] = scope
    return brief


def select_executor_type(task_brief: dict[str, Any]) -> str:
    initial = task_brief.get("initial_input")
    if isinstance(initial, dict) and isinstance(initial.get("deterministic_action"), dict):
        return "deterministic"
    return "agent"

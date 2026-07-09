"""
自进化开发规划模块。

使用 LLM 根据用户需求生成结构化的补丁计划（patch plan）。
计划包含操作列表、测试方案等，最终由 PatchExecutor 在各槽位中执行。
仅对 FORBIDDEN_PATHS 中的核心文件操作标记 not_allowed_yet，其余操作可执行。
"""

import json
import logging
from typing import Any

from json_repair import repair_json

from aiive.core.llm_client import LLMClient, LLMResponse

logger = logging.getLogger(__name__)

# LLM 规划提示词模板：指导 LLM 生成结构化的补丁计划 JSON
PLAN_PROMPT = (
    "You are an AI system architect. Given a user requirement, produce a structured "
    "patch plan for the AIive personal agent project. DO NOT apply any changes. "
    "Only generate a plan.\n\n"
    "## Project Structure Available\n"
    "backend/aiive/ - FastAPI backend (api/, core/, db/, runtime/, memory/, tools/, mcp/, supervisor/, selfdev/)\n"
    "frontend/ - React + Vite + TypeScript\n"
    "tests/unit/backend/ - pytest unit tests\n"
    "docs/ - design docs\n\n"
    "## Rules\n"
    "- NEVER suggest modifying active slot or inactive slot in this phase\n"
    "- If an operation requires applying patches, mark it not_allowed_yet\n"
    "- If an operation involves deletion, declare safe_delete_scope\n"
    "- If schema changes are needed, mark requires_schema_change\n"
    "- External tool installs must go through MCP sandbox\n\n"
    "## Output Format (JSON only, no markdown)\n"
    "{{\n"
    '  "goal_summary": "one-line summary",\n'
    '  "operations": [\n'
    '    {{\n'
    '      "operation": "add_file | modify_file | delete_file",\n'
    '      "target_file": "path/to/file.py",\n'
    '      "reason": "why this change is needed",\n'
    '      "risk_notes": "potential risks if any",\n'
    '      "requires_schema_change": false,\n'
    '      "safe_delete_scope": "optional scope id",\n'
    '      "not_allowed_yet": false\n'
    "    }}\n"
    "  ],\n"
    '  "test_plan": "how to test the changes",\n'
    '  "requires_schema_change": false\n'
    "}}\n\n"
    "User Requirement: {goal}\n\n"
    "Output ONLY valid JSON:"
)

# 允许的操作类型白名单
ALLOWED_OPERATIONS = {"add_file", "modify_file", "delete_file"}
# 禁止修改的核心文件路径
FORBIDDEN_PATHS = {"main.py", "config.py", "db/base.py", "db/models.py"}


class SelfDevPlanner:
    """自进化开发规划器，使用 LLM 生成补丁计划。"""

    def __init__(self, llm_client: LLMClient):
        """
        初始化规划器。

        参数:
            llm_client: LLM 客户端实例，用于调用大模型生成计划。
        """
        self._llm_client = llm_client

    def plan(self, goal: str, trace_id: str | None = None) -> dict[str, Any]:
        """
        根据用户目标生成结构化补丁计划。

        参数:
            goal: 用户需求描述文本。
            trace_id: 追踪 ID，可选。

        返回:
            包含 goal_summary、operations、test_plan 等字段的计划字典。
        """
        prompt = PLAN_PROMPT.format(goal=goal)
        messages = [{"role": "user", "content": prompt}]

        try:
            response: LLMResponse = self._llm_client.chat(
                messages, trace_id=trace_id, temperature=0.2
            )
        except Exception:
            logger.exception("自进化计划生成LLM调用失败")
            raise
        return self._parse(response.content)

    def _parse(self, raw: str) -> dict[str, Any]:
        """
        解析 LLM 返回的原始文本为计划字典。

        参数:
            raw: LLM 原始响应文本。

        返回:
            解析并验证后的计划字典。解析失败时返回空计划。
        """
        try:
            text = raw.strip()
            if not text:
                return self._empty_plan()
            # 移除可能的 markdown 代码块标记
            if text.startswith("```"):
                lines = text.split("\n")
                inner_lines = lines[1:]
                if inner_lines and inner_lines[-1].strip() == "```":
                    inner_lines = inner_lines[:-1]
                text = "\n".join(inner_lines).strip()
                if not text:
                    return self._empty_plan()
            try:
                plan = json.loads(text)
            except json.JSONDecodeError:
                repaired = repair_json(text)
                plan = json.loads(repaired)
        except (json.JSONDecodeError, ValueError):
            logger.warning("自进化计划JSON解析失败", exc_info=True)
            return self._empty_plan()

        return self._validate(plan)

    def _validate(self, plan: dict) -> dict[str, Any]:
        """
        验证并规范化计划内容，包括：
        - 为缺失字段设置默认值
        - 修正非法操作类型
        - 标记禁止修改的核心文件
        - 对 FORBIDDEN_PATHS 中的核心文件标记 not_allowed_yet

        参数:
            plan: 待验证的计划字典。

        返回:
            验证并规范化后的计划字典。
        """
        operations = plan.get("operations", [])

        for op in operations:
            # 为每个操作设置默认字段值
            op.setdefault("operation", "add_file")
            op.setdefault("not_allowed_yet", False)
            op.setdefault("requires_schema_change", False)
            op.setdefault("safe_delete_scope", None)
            op.setdefault("risk_notes", "")

            # 修正不在白名单中的操作类型
            if op["operation"] not in ALLOWED_OPERATIONS:
                op["operation"] = "add_file"

            # 标记禁止修改的核心文件路径
            for forbidden in FORBIDDEN_PATHS:
                if forbidden in op.get("target_file", ""):
                    op["not_allowed_yet"] = True
                    op["risk_notes"] = (op.get("risk_notes", "") +
                        " CORE_FILE_PROTECTED: cannot modify core infrastructure in this phase.")

        plan.setdefault("test_plan", "")
        plan.setdefault("goal_summary", plan.get("goal_summary", ""))
        plan.setdefault("requires_schema_change", any(
            op.get("requires_schema_change") for op in operations
        ))

        return plan

    def _empty_plan(self) -> dict[str, Any]:
        """返回一个空计划，用于 LLM 解析失败时的降级处理。"""
        return {
            "goal_summary": "Could not generate plan",
            "operations": [],
            "test_plan": "",
            "requires_schema_change": False,
        }

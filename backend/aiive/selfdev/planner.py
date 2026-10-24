"""
自进化开发规划模块。

使用 LLM 根据用户需求生成结构化的补丁计划（patch plan）。
计划包含操作列表、测试方案等，最终由 PatchExecutor 在各槽位中执行。
非法操作、缺少完整内容及 Trusted Core 路径都会标记为不可执行。
"""

import json
import logging
from typing import Any

from json_repair import repair_json

from aiive.core.llm_client import LLMClient, LLMResponse
from aiive.prompts import get_prompt_registry
from aiive.selfdev.trusted_core import TRUSTED_CORE_PREFIXES, protected_reason

logger = logging.getLogger(__name__)

# 允许的操作类型白名单
ALLOWED_OPERATIONS = {"add_file", "modify_file", "delete_file"}
# 兼容旧导入名称；实际判定统一由 Trusted Core 策略完成。
FORBIDDEN_PATHS = set(TRUSTED_CORE_PREFIXES)


class SelfDevPlanner:
    """自进化开发规划器，使用 LLM 生成补丁计划。"""

    def __init__(self, llm_client: LLMClient):
        """
        初始化规划器。

        参数:
            llm_client: LLM 客户端实例，用于调用大模型生成计划。
        """
        self._llm_client: LLMClient = llm_client

    def plan(
        self,
        goal: str,
        trace_id: str | None = None,
        *,
        context: Any = None,
        previous_plan: dict[str, Any] | None = None,
        failure_feedback: str | None = None,
    ) -> dict[str, Any]:
        """
        根据用户目标生成结构化补丁计划。

        参数:
            goal: 用户需求描述文本。
            trace_id: 追踪 ID，可选。
            context: 只读代码上下文（ProjectContext 或其字典形式），可选。
            previous_plan: 上一轮已应用但测试失败的补丁计划，可选。
            failure_feedback: 上一轮测试失败的真实输出，可选。

        返回:
            包含 goal_summary、operations、test_plan 等字段的计划字典。
        """
        prompt = get_prompt_registry().render(
            "selfdev.patch_plan",
            goal=self._compose_goal(goal, context, previous_plan, failure_feedback),
        ).content
        messages = [{"role": "user", "content": prompt}]

        try:
            response: LLMResponse = self._llm_client.chat(
                messages, trace_id=trace_id, temperature=0.2
            )
        except Exception:
            logger.exception("自进化计划生成LLM调用失败")
            raise
        return self._parse(response.content)

    @staticmethod
    def _compose_goal(
        goal: str,
        context: Any = None,
        previous_plan: dict[str, Any] | None = None,
        failure_feedback: str | None = None,
    ) -> str:
        """把目标、代码上下文与失败反馈拼成单个提示词变量。

        复用 ``selfdev.patch_plan`` 模板（不新增提示词条目），
        但把只读代码与真实测试错误作为"待规划数据"一并交给模型。
        """
        sections: list[str] = [str(goal).strip()]
        if context:
            from aiive.selfdev.project_reader import format_context_for_prompt

            sections.append(
                "# 已读取的仓库代码（只读上下文，来自当前仓库）\n\n"
                + format_context_for_prompt(context)
            )
        if previous_plan:
            operations = previous_plan.get("operations")
            lines: list[str] = []
            if isinstance(operations, list):
                for operation in operations[:20]:
                    if not isinstance(operation, dict):
                        continue
                    lines.append(
                        f"- {operation.get('operation', '')} "
                        + f"{operation.get('target_file', '')}"
                    )
            sections.append(
                "# 上一轮补丁（已应用，但测试未通过）\n\n"
                + ("\n".join(lines) if lines else "（上一轮没有任何可执行操作）")
            )
        if failure_feedback:
            sections.append(
                "# 测试失败的真实输出（必须据此修正，不要重复提交相同补丁）\n\n"
                + "<<<TEST_OUTPUT\n"
                + str(failure_feedback)[-8000:]
                + "\nTEST_OUTPUT>>>"
            )
        return "\n\n".join(sections)

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

        if not isinstance(plan, dict):
            logger.warning("自进化计划顶层不是 JSON object")
            return self._empty_plan()
        return self._validate(plan)

    def _validate(self, plan: dict[str, Any]) -> dict[str, Any]:
        """
        验证并规范化计划内容，包括：
        - 为缺失字段设置默认值
        - 阻断非法操作类型和缺失完整内容的写操作
        - 对 Trusted Core 核心文件标记 not_allowed_yet

        参数:
            plan: 待验证的计划字典。

        返回:
            验证并规范化后的计划字典。
        """
        raw_operations = plan.get("operations", [])
        if not isinstance(raw_operations, list):
            logger.warning("自进化计划 operations 不是数组")
            return self._empty_plan()
        operations: list[dict[str, Any]] = []

        for raw_operation in raw_operations:
            if not isinstance(raw_operation, dict):
                logger.warning("忽略非对象自进化操作: %r", raw_operation)
                continue
            op = dict(raw_operation)
            # 为每个操作设置默认字段值
            op.setdefault("operation", "add_file")
            op.setdefault("not_allowed_yet", False)
            op.setdefault("requires_schema_change", False)
            op.setdefault("safe_delete_scope", None)
            op.setdefault("risk_notes", "")

            # 保留 fail-closed 语义：未知类型不可降级为一个可执行写操作。
            if op["operation"] not in ALLOWED_OPERATIONS:
                original = str(op["operation"])
                op["operation"] = "add_file"
                op["not_allowed_yet"] = True
                op["risk_notes"] = (
                    op.get("risk_notes", "")
                    + f" UNKNOWN_OPERATION: {original}; operation blocked."
                )

            # 契约校验：add_file / modify_file 必须携带非空完整 content，
            # 否则执行阶段会把文件写空。缺失时标记为不可执行。
            if op["operation"] in ("add_file", "modify_file"):
                content = op.get("content")
                if not isinstance(content, str) or not content.strip():
                    op["not_allowed_yet"] = True
                    op["risk_notes"] = (
                        op.get("risk_notes", "")
                        + " MISSING_CONTENT: add_file/modify_file requires full non-empty content; "
                        + "operation blocked to avoid writing empty files."
                    )

            # Trusted Core 使用规范化仓库路径精确判定，禁止子串误报和 ../ 绕过。
            reason = protected_reason(str(op.get("target_file", "")))
            if reason:
                op["not_allowed_yet"] = True
                op["risk_notes"] = (
                    op.get("risk_notes", "")
                    + f" CORE_FILE_PROTECTED: {reason}."
                )
            operations.append(op)

        plan = dict(plan)
        plan["operations"] = operations
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

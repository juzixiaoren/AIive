"""
运行时层 - LangGraph Agent 图编排（替代 agent_loop.py）。

全面使用 LangGraph StateGraph + ToolNode 原生机制处理工具调用循环。
所有工具调用由 LLM + bind_tools() + LangGraph 原生 ToolNode + policy_check 架构处理。

架构流程：
  START -> assistant -> policy_check -> [tools -> assistant] -> END

- assistant：带 bind_tools 的 LLM 节点，生成带有原生 tool_calls 的 AIMessage
- policy_check：基于 ToolRegistry 元数据校验 tool_calls（非关键词匹配）
- tools：LangGraph 原生 ToolNode，执行工具并生成 ToolMessage

核心职责：
1. 构建 LangChain LLM 并绑定工具
2. 构建包含身份、记忆、任务的系统提示
3. 组装对话历史
4. 构建并运行 LangGraph StateGraph
5. 记录事件日志并完成持久化（finalize）
"""

from __future__ import annotations

import asyncio
import hashlib
import json as _json
import logging
import operator
import uuid as _uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable

logger = logging.getLogger(__name__)

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from pydantic import SecretStr
from sqlalchemy.orm import Session
from typing_extensions import TypedDict

from aiive.config import settings
from aiive.context.run_context import RunContext
from aiive.core.action_planner import ActionPlanner, MemorySignalDecision
from aiive.core.llm_client import normalize_llm_error
from aiive.core.llm_client import LLMClient
from aiive.memory.extraction_policy import MemoryExtractionPolicy, MemorySignalAction
from aiive.prompts import get_prompt_registry
from aiive.runtime.context_assembler import ContextSnapshotData, ContextSnapshotItem
from aiive.runtime.execution_context import TurnExecutionContext
from aiive.runtime.policy_engine import check_tool_calls, PolicyAction
from aiive.runtime.thread_state import ThreadState
from aiive.runtime.action_cards import ActionCard, PendingOperation
from aiive.runtime.tool_executor import ToolCallRecord, build_action_cards, build_pending_operations
from aiive.runtime.trace import Trace
from aiive.tools.langchain_adapter import build_langchain_tools
from aiive.tools.registry import ToolRegistry, get_tool_registry
from aiive.runtime.tool_normalizer import ToolResultNormalizer
from aiive.runtime.token_counter import LiteLLMTokenCounter
from aiive.runtime.working_state import WorkingStateService
from aiive.runtime.message_normalizer import normalize_tool_call, normalize_tool_result


def _ws_commit(mutate: "Callable[[Session], None]") -> None:
    """WorkingState 写入：独立短事务。

    惰性导入 SessionLocal，使测试环境对 aiive.db.base.SessionLocal 的 monkeypatch
    能生效（避免直连 Postgres）。写入失败不阻断主流程，仅记录告警。
    """
    from aiive.db.base import SessionLocal
    db = SessionLocal()
    try:
        mutate(db)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("[TRACE:graph] WorkingState 写入失败（已回滚）")
    finally:
        db.close()


# 模块级 AgentState，必须定义在模块层级，以便 get_type_hints()
# 能从实例方法内部的闭包中解析类型
class _AgentState(TypedDict):
    """LangGraph 状态字典（模块级定义，支持类型解析）。"""
    messages: Annotated[list[Any], add_messages]
    tool_records: Annotated[list[dict[str, Any]], operator.add]
    tool_validation_ok: bool
    tool_validation_attempts: int
    tool_validation_records: list[dict[str, Any]]


@dataclass
class _StreamToolCallMatcher:
    """将 LangGraph 流式工具事件关联到模型生成的真实工具调用标识。"""

    pending: list[dict[str, Any]] = field(default_factory=list)
    run_ids: dict[str, str] = field(default_factory=dict)

    def register_batch(self, tool_calls: list[Any]) -> None:
        """登记一个 tools 节点即将执行的模型工具调用批次。"""
        self.pending.extend(
            {
                "tool_call_id": str(call.get("id", "") or ""),
                "name": str(call.get("name", "") or ""),
                "params": call.get("args", {}) if isinstance(call.get("args"), dict) else {},
            }
            for call in tool_calls
            if isinstance(call, dict)
        )

    def start(
        self,
        run_id: str,
        name: str,
        params: dict[str, Any],
        event_tool_call_id: str = "",
    ) -> str:
        """优先按事件调用标识匹配，缺失时按名称、参数和登记顺序回退。"""
        if event_tool_call_id:
            for index, call in enumerate(self.pending):
                if call["tool_call_id"] == event_tool_call_id:
                    self.pending.pop(index)
                    self.run_ids[run_id] = event_tool_call_id
                    return event_tool_call_id
        for index, call in enumerate(self.pending):
            if call["name"] == name and call["params"] == params:
                matched = self.pending.pop(index)
                self.run_ids[run_id] = matched["tool_call_id"]
                return matched["tool_call_id"]
        return ""

    def finish(self, run_id: str, fallback_id: str = "") -> str:
        """返回工具结束事件对应的真实工具调用标识。"""
        return self.run_ids.pop(run_id, "") or fallback_id


# ---------------------------------------------------------------------------
# 模块级工具函数
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolExecutionReceipt:
    """工具返回内容解析后的统一执行回执。"""

    content: str
    ok: bool
    status: str
    execution_status: str = ""
    operation_id: str = ""
    error_type: str = ""


def parse_tool_execution_receipt(content: str) -> ToolExecutionReceipt:
    """将 ToolMessage 内容解析为唯一的工具执行状态契约。"""
    is_error = content.startswith("Error:") if content else False
    execution_status = ""
    operation_id = ""
    error_type = ""
    if not is_error and content.startswith("{"):
        try:
            parsed = _json.loads(content)
            if isinstance(parsed, dict):
                execution_status = str(parsed.get("execution_status", "") or "")
                operation_id = str(parsed.get("operation_id", "") or "")
                error_type = str(parsed.get("error_type", "") or "")
                if not parsed.get("ok", True):
                    is_error = True
        except (ValueError, TypeError):
            pass
    status = (
        "execution_unknown"
        if execution_status in ("queued", "running", "execution_unknown")
        else ("failed" if is_error else "completed")
    )
    return ToolExecutionReceipt(
        content=content,
        ok=not is_error,
        status=status,
        execution_status=execution_status,
        operation_id=operation_id,
        error_type=error_type,
    )


class GraphExecutionInterrupted(Exception):
    """流式图执行中断，并携带中断前已经确认的工具事实。"""

    def __init__(
        self,
        error: Exception,
        trace_id: str,
        partial_reply: str,
        tool_records: list["ToolRecord"],
    ) -> None:
        self.error: Exception = error
        self.trace_id: str = trace_id
        self.partial_reply: str = partial_reply
        self.tool_records: list[ToolRecord] = tool_records
        super().__init__(str(error))


def _build_runtime_identity(runtime_identity: dict[str, str] | None) -> str:
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
    if len(lines) == 1:
        return ""
    return "\n".join(lines) + "\n"


def _truncate(text: str, max_len: int) -> str:
    """截断文本到指定长度，超出部分用 … 表示。"""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "…"


def _bound_stream_tool_content(content: str, normalizer: Any | None) -> str:
    """按 ToolResultNormalizer 内联上限（token）折算的字符上限截断流式工具输出。

    用于 SSE tool_result 事件与中断工具事实持久化两个消费点，
    防止原始大结果无界推送/落库。按 ~4 字符/token 保守折算。
    """
    from aiive.runtime.tool_normalizer import SINGLE_TOOL_RESULT_INLINE_LIMIT
    limit_tokens = getattr(normalizer, "SINGLE_RESULT_INLINE_LIMIT", None) or SINGLE_TOOL_RESULT_INLINE_LIMIT
    max_chars = int(limit_tokens) * 4
    if len(content) <= max_chars:
        return content
    return content[:max_chars] + "\n…[工具结果过大已截断，完整内容见 Artifact 引用]"


def flatten_tool_result(result: object) -> str:
    """从嵌套的 {ok: bool, result: str} 结构中提取实际结果字符串。"""
    if isinstance(result, dict) and "result" in result:
        return result["result"]
    return str(result)



# ---------------------------------------------------------------------------
# Phase 0.5A: AgentGraphResult / ToolRecord (pure data DTOs)
# ---------------------------------------------------------------------------


@dataclass
class ToolRecord:
    """单个工具调用的完整记录。"""
    tool_call_id: str = ""        # LLM 生成的 ID (如 "call_abc123")
    batch_index: int = 0          # 属于第几个 AIMessage(tool_calls=[...])
    name: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    status: str = "completed"     # "completed" | "failed" | "execution_unknown" | "blocked"
    order_index: int = 0          # 全局执行顺序
    reason: str = ""              # blocked 等状态的原因说明


@dataclass
class LLMUsageStats:
    """一次 Agent 图执行中所有主模型调用的聚合 token 用量。"""

    calls_reported: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def cache_hit_ratio(self) -> float | None:
        if self.input_tokens <= 0:
            return None
        return round(self.cache_read_tokens / self.input_tokens, 4)

    def as_dict(self) -> dict[str, int | float | None]:
        return {
            "calls_reported": self.calls_reported,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cache_hit_ratio": self.cache_hit_ratio,
        }


@dataclass
class AgentGraphResult:
    """AgentGraph 执行完成后返回的纯数据结构。不含任何 DB 句柄。"""

    reply: str = ""
    trace_id: str = ""
    user_message: str = ""
    tool_records: list[ToolRecord] = field(default_factory=list)
    action_cards: list[ActionCard] = field(default_factory=list)
    pending_operations: list[PendingOperation] = field(default_factory=list)
    pending_approvals: list[dict[str, Any]] = field(default_factory=list)
    context_snapshot: ContextSnapshotData = field(default_factory=ContextSnapshotData)
    memory_signal: Any = None  # MemorySignalDecision
    llm_usage: LLMUsageStats = field(default_factory=LLMUsageStats)


# ---------------------------------------------------------------------------
# AgentGraph 类
# ---------------------------------------------------------------------------


class AgentGraph:
    """Agent 图编排：替代 AgentLoop，全面使用 LangGraph 原生机制。

    负责每个对话轮次的完整生命周期：
    上下文构建 → LangGraph 图执行 → 结果持久化 → action_cards 生成。

    统一入口为 TurnExecutionService.execute_turn（Phase 1 bounded context），
    通过 _execute_graph 执行图推理（ContextAssembler 预组装消息）。

    Attributes:
        _llm_client: AIive LLMClient 实例
        _db: SQLAlchemy 数据库会话
        _thread_state: 线程状态管理器
        _action_planner: 动作规划器（记忆信号分类）
    """

    def __init__(self, llm_client: LLMClient, db: Session):
        self._llm_client: LLMClient = llm_client
        self._db: Session = db
        self._thread_state: ThreadState = ThreadState(db)
        self._action_planner: ActionPlanner = ActionPlanner(llm_client)

    # ------------------------------------------------------------------
    # LangChain LLM 工厂
    # ------------------------------------------------------------------

    def _build_langchain_llm(self) -> ChatOpenAI:
        """使用 AIive LLMClient 配置构建 LangChain ChatOpenAI 实例。"""
        return ChatOpenAI(
            model=self._llm_client.default_model,
            api_key=SecretStr(self._llm_client.api_key),
            base_url=self._llm_client.base_url,
            temperature=0,
            timeout=self._llm_client.timeout_seconds,
            streaming=True,
            stream_usage=settings.aiive_llm_stream_usage,
        )

    @staticmethod
    def _collect_llm_usage(messages: list[Any]) -> LLMUsageStats:
        """从最终图状态聚合各次 AIMessage 的 usage 与提示缓存命中。

        LangChain 将 OpenAI/Qwen 的 cached_tokens 归一化到
        input_token_details.cache_read；若兼容端点只保留原始 usage
        （如部分 DeepSeek 版本），再从 response_metadata 补读缓存字段。
        """
        stats = LLMUsageStats()
        for message in messages:
            if not isinstance(message, AIMessage):
                continue
            usage = getattr(message, "usage_metadata", None)
            response_metadata = getattr(message, "response_metadata", None)
            raw_usage: Any = {}
            if isinstance(response_metadata, dict):
                candidate = response_metadata.get("token_usage", response_metadata.get("usage", {}))
                if isinstance(candidate, dict):
                    raw_usage = candidate

            if isinstance(usage, dict):
                stats.calls_reported += 1
                stats.input_tokens += int(usage.get("input_tokens", 0) or 0)
                stats.output_tokens += int(usage.get("output_tokens", 0) or 0)
                stats.total_tokens += int(usage.get("total_tokens", 0) or 0)
                details = usage.get("input_token_details", {})
                if isinstance(details, dict):
                    stats.cache_read_tokens += int(
                        details.get("cache_read", details.get("cached_tokens", 0)) or 0
                    )
                    stats.cache_write_tokens += int(
                        details.get("cache_creation", details.get("cache_write", 0)) or 0
                    )
            elif raw_usage:
                normalized = LLMClient._normalize_usage(raw_usage)  # pyright: ignore[reportPrivateUsage]
                stats.calls_reported += 1
                stats.input_tokens += normalized["prompt_tokens"]
                stats.output_tokens += normalized["completion_tokens"]
                stats.total_tokens += normalized["total_tokens"]

            # raw usage 仅用于补足 LangChain 尚未映射的缓存字段，避免重复累计。
            if raw_usage:
                normalized = LLMClient._normalize_usage(raw_usage)  # pyright: ignore[reportPrivateUsage]
                if isinstance(usage, dict):
                    details = usage.get("input_token_details", {})
                    has_cache_read = isinstance(details, dict) and (
                        "cache_read" in details or "cached_tokens" in details
                    )
                    has_cache_write = isinstance(details, dict) and (
                        "cache_creation" in details or "cache_write" in details
                    )
                    if not has_cache_read:
                        stats.cache_read_tokens += normalized.get("cache_read_tokens", 0)
                    if not has_cache_write:
                        stats.cache_write_tokens += normalized.get("cache_write_tokens", 0)

        if stats.calls_reported:
            logger.info(
                "[TRACE:graph] LLM usage: calls=%d input=%d output=%d total=%d "
                + "cache_read=%d cache_write=%d hit_ratio=%s",
                stats.calls_reported,
                stats.input_tokens,
                stats.output_tokens,
                stats.total_tokens,
                stats.cache_read_tokens,
                stats.cache_write_tokens,
                stats.cache_hit_ratio,
            )
        return stats

    # ------------------------------------------------------------------
    # 系统提示构建
    # ------------------------------------------------------------------

    @staticmethod
    def _build_stable_contract(
        runtime_identity: dict[str, str] | None = None,
        policies: list[dict[str, Any]] | None = None,
    ) -> str:
        """构建稳定系统契约（Kernel Contract）：基础指令 + 身份 + 策略。

        仅包含不可变/稳定的指令与精确 key 身份、policy，不注入普通长期记忆列表，
        也不每轮注入全部 active/due tasks（V2 §十二）。动态记忆由 Automatic Recall
        在调用链后置注入。
        """
        parts = [get_prompt_registry().render("agent.stable_contract").content]

        identity_text = _build_runtime_identity(runtime_identity)
        if identity_text:
            parts.append(identity_text)

        if policies:
            parts.append("\n## Active Policies (user-defined rules — instructions, not memory)")
            for p in policies:
                parts.append(f"- {p.get('content', '')}")

        return "\n".join(parts)


    @staticmethod
    def _build_post_context_items(
        reply: str,
        records: list[dict[str, Any]],
    ) -> tuple[list[ContextSnapshotItem], dict[str, str]]:
        """构建后执行上下文项：Agent 输出、工具调用和工具结果。

        返回 (context_items, full_contents)：
        - context_items: 折叠态展示的截断预览（~20 字）
        - full_contents: item_id → 完整文本的映射，供前端展开时懒加载
        """
        items: list[ContextSnapshotItem] = []
        full: dict[str, str] = {}

        # Agent 输出
        if reply:
            item_id = "agent_output"
            preview = reply[:20] + ("..." if len(reply) > 20 else "")
            items.append(ContextSnapshotItem(
                item_id=item_id, kind="agent_output", source="agent",
                trust_level="trusted", content_preview=preview,
                token_estimate=max(1, len(reply) // 4),
            ))
            full[item_id] = reply

        # 工具调用 + 工具结果
        for i, r in enumerate(records):
            name = r.get("name", "unknown")
            params = r.get("params", {})
            result = r.get("result", {})
            status = r.get("status", "unknown")

            # 工具调用
            call_id = f"tool_call:{i}"
            params_text = _json.dumps(params, ensure_ascii=False)
            preview = f"{name}({_truncate(params_text, 20)})"
            items.append(ContextSnapshotItem(
                item_id=call_id, kind="tool_call", source="tools",
                trust_level="trusted", content_preview=preview,
                token_estimate=max(1, len(params_text) // 4),
            ))
            full[call_id] = f"名称: {name}\n参数: {params_text}"

            # 工具结果
            result_id = f"tool_result:{i}"
            result_text = _json.dumps(result, ensure_ascii=False)
            preview = f"[{status}] {_truncate(result_text, 20)}"
            items.append(ContextSnapshotItem(
                item_id=result_id, kind="tool_result", source="tools",
                trust_level="trusted", content_preview=preview,
                token_estimate=max(1, len(result_text) // 4),
            ))
            full[result_id] = f"名称: {name}\n状态: {status}\n结果: {result_text}"

        return items, full

    # ------------------------------------------------------------------
    # 图构建（非流式）
    # ------------------------------------------------------------------

    def _build_graph(
        self,
        llm_with_tools: Runnable[Any, Any],
        tools: list[StructuredTool],
        registry: ToolRegistry | None,
        trace_id: str = "",
        _thread_id: str = "",
        _model: str = "",
        normalizer: "ToolResultNormalizer | None" = None,
        ws_service: "WorkingStateService | None" = None,
        thread_id: str = "",
        turn_record_id: str = "",
        execution_id: str = "",
    ) -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]]]:
        """构建并返回编译后的 StateGraph、工具记录引用列表和待审批列表。

        Returns:
            (compiled_graph, tool_records, pending_approvals) 三元组
        """
        tool_records: list[dict[str, Any]] = []
        _pending_approval_list: list[dict[str, Any]] = []
        args_by_id: dict[str, dict[str, Any]] = {}
        from aiive.config import settings
        from aiive.tools.tool_validation import validate_tool_params

        def _assistant(state: _AgentState) -> dict[str, Any]:
            """assistant 节点：LLM 推理，可生成 tool_calls。"""
            msgs = state["messages"]
            logger.info("[TRACE:graph] ASSISTANT(ns) msgs_count=%d", len(msgs))
            resp = llm_with_tools.invoke(msgs)
            tc_count = len(getattr(resp, "tool_calls", None) or [])
            content_len = len(str(resp.content or ""))
            logger.info("[TRACE:graph] ASSISTANT(ns) tool_calls=%d content_len=%d", tc_count, content_len)
            # LLM 返回既无内容也无工具调用属于异常空响应，记录以便定位根因
            # （模型侧偶发、工具绑定异常、streaming 兼容问题等）。
            if tc_count == 0 and content_len == 0:
                logger.warning(
                    "[TRACE:graph] ASSISTANT(ns) LLM 返回空响应（无内容且无工具调用）: msgs_count=%d",
                    len(msgs),
                )
            if tc_count > 0:
                for tc in resp.tool_calls:
                    logger.info("[TRACE:graph] ASSISTANT(ns) tc: name=%s args=%s", tc.get("name", "?"), tc.get("args", {}))
            # 收集 tool_call 参数，供后续 tool_result 关联
            for tc in getattr(resp, "tool_calls", None) or []:
                args_by_id[tc.get("id", "") or ""] = tc.get("args", {})
            return {"messages": [resp]}

        def _policy_check(state: _AgentState) -> str:
            """策略检查节点：根据 ToolRegistry 元数据校验 tool_calls。"""
            nonlocal _pending_approval_list
            msgs = state.get("messages", [])
            if not msgs:
                logger.info("[TRACE:graph] POLICY_CHECK(ns): no messages → finalize")
                return "finalize"
            last_msg = msgs[-1]
            if not isinstance(last_msg, AIMessage) or not last_msg.tool_calls:
                logger.info("[TRACE:graph] POLICY_CHECK(ns): no tool_calls → finalize")
                return "finalize"
            tool_calls_raw = [
                {"name": tc["name"], "args": tc.get("args", {}), "id": tc.get("id", "")}
                for tc in last_msg.tool_calls
            ]
            logger.info("[TRACE:graph] POLICY_CHECK(ns): %d tool_calls=%s", len(tool_calls_raw), [tc["name"] for tc in tool_calls_raw])
            result = check_tool_calls(tool_calls_raw, registry)
            if result.action == PolicyAction.CONFIRM:
                _pending_approval_list[:] = [
                    {"name": tc["name"], "args": tc.get("args", {}), "id": tc.get("id", ""),
                     "safe_tool_calls": tool_calls_raw}
                    for tc in last_msg.tool_calls
                ]
                logger.info("[TRACE:graph] POLICY_CHECK(ns): action=CONFIRM → confirm (%d tools)", len(_pending_approval_list))
                return "confirm"
            logger.info("[TRACE:graph] POLICY_CHECK(ns): action=%s → %s", result.action, "finalize" if result.action == PolicyAction.BLOCK else "continue")
            if result.action == PolicyAction.BLOCK:
                # 记录被阻止的工具事实（tool_blocked 卡片），避免阻断原因丢失后
                # 被误报为 empty_llm_response。
                blocked_set = set(result.blocked_tools)
                batch_index = max(
                    0,
                    sum(1 for m in msgs if isinstance(m, AIMessage) and m.tool_calls) - 1,
                )
                for tc in tool_calls_raw:
                    if tc["name"] not in blocked_set:
                        continue
                    tool_records.append({
                        "name": tc["name"],
                        "params": tc.get("args", {}),
                        "result": {"ok": False, "result": f"blocked: {result.reason}"},
                        "status": "blocked",
                        "reason": result.reason,
                        "trace_id": trace_id,
                        "tool_call_id": str(tc.get("id", "") or ""),
                        "batch_index": batch_index,
                    })
                return "finalize"
            return "continue"

        def _validate_tools(state: _AgentState) -> dict[str, Any]:
            """在副作用发生前验证整批工具参数，并生成可供模型纠正的 ToolMessage。"""
            msgs = state.get("messages", [])
            last_msg = msgs[-1] if msgs else None
            if not isinstance(last_msg, AIMessage) or not last_msg.tool_calls:
                return {
                    "tool_validation_ok": True,
                    "tool_validation_attempts": 0,
                    "tool_validation_records": [],
                }
            # 单元测试可直接传入临时 StructuredTool 而不提供 Registry；生产路径
            # 始终提供 Registry，并由 registry.execute 再做一次纵深校验。
            if registry is None:
                return {
                    "tool_validation_ok": True,
                    "tool_validation_attempts": 0,
                    "tool_validation_records": [],
                }

            results: dict[str, Any] = {}
            has_invalid = False
            for tc in last_msg.tool_calls:
                name = str(tc.get("name", "") or "")
                reg = registry.get(name)
                if reg is None:
                    continue  # 未注册工具已由 policy_check 拦截
                result = validate_tool_params(reg, tc.get("args", {}))
                results[str(tc.get("id", "") or "")] = result
                has_invalid = has_invalid or not result.ok

            if not has_invalid:
                return {
                    "tool_validation_ok": True,
                    "tool_validation_attempts": 0,
                    "tool_validation_records": [],
                }

            attempts = int(state.get("tool_validation_attempts", 0) or 0) + 1
            batch_index = max(
                0,
                sum(1 for message in msgs if isinstance(message, AIMessage) and message.tool_calls) - 1,
            )
            feedback: list[ToolMessage] = []
            validation_records: list[dict[str, Any]] = []
            for tc in last_msg.tool_calls:
                call_id = str(tc.get("id", "") or "")
                name = str(tc.get("name", "") or "")
                validation = results.get(call_id)
                if validation is not None and not validation.ok:
                    payload = validation.error_payload(name)
                    reason = "工具参数本地校验失败"
                else:
                    payload = {
                        "ok": False,
                        "error_type": "batch_aborted_due_to_invalid_arguments",
                        "retryable": True,
                        "tool_name": name,
                        "instruction": "同批其他工具参数无效，本批未执行；请重新生成完整工具调用批次。",
                    }
                    reason = "同批参数校验失败，工具未执行"
                content = _json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                feedback.append(ToolMessage(
                    content=content,
                    tool_call_id=call_id,
                    name=name,
                ))
                record = {
                    "name": name,
                    "params": tc.get("args", {}),
                    "result": {
                        "ok": False,
                        "result": content,
                        "error_type": str(payload.get("error_type", "")),
                    },
                    "status": "parse_error",
                    "reason": reason,
                    "trace_id": trace_id,
                    "tool_call_id": call_id,
                    "batch_index": batch_index,
                }
                validation_records.append(record)
            logger.info(
                "[TRACE:graph] VALIDATE_TOOLS(ns): invalid batch attempts=%d tools=%s",
                attempts,
                [tc.get("name", "") for tc in last_msg.tool_calls],
            )
            return {
                "messages": feedback,
                "tool_validation_ok": False,
                "tool_validation_attempts": attempts,
                "tool_validation_records": validation_records,
            }

        def _route_after_validation(state: _AgentState) -> str:
            if state.get("tool_validation_ok", True):
                return "tools"
            attempts = int(state.get("tool_validation_attempts", 0) or 0)
            if attempts > settings.tool_argument_max_retries:
                return "validation_abort"
            return "assistant"

        def _validation_abort(state: _AgentState) -> dict[str, Any]:
            final_records = list(state.get("tool_validation_records", []))
            tool_records.extend(final_records)
            return {
                "messages": [AIMessage(
                    content=(
                        "工具参数连续校验失败，已停止执行以避免错误操作。"
                        "请补充或确认必要参数后再试。"
                    )
                )],
                "tool_records": final_records,
            }

        def _confirm_node(state: _AgentState) -> dict[str, Any]:  # pyright: ignore[reportUnusedParameter]
            """确认节点：冻结服务端工具调用，等待 Turn 最终事务持久化。

            CONFIRM 工具不在此节点执行；审批事实与 action card 在 Turn 完成时
            原子落库，之后只能由审批 API 读取并执行。
            """
            if not _pending_approval_list:
                return {}
            if registry is None:
                raise RuntimeError("审批节点缺少 ToolRegistry")
            for pa in _pending_approval_list:
                tc_name = str(pa.get("name", "") or "")
                tc_args = pa.get("args", {}) if isinstance(pa.get("args"), dict) else {}
                reg = registry.get(tc_name)
                if reg is None or not reg.safety.descriptor_hash:
                    raise RuntimeError(f"工具 {tc_name} 缺少有效的 descriptor hash")
                canonical_args = _json.dumps(
                    tc_args, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
                )
                pa["approval_id"] = str(_uuid.uuid4())
                pa["tool_args_hash"] = hashlib.sha256(canonical_args.encode("utf-8")).hexdigest()
                pa["descriptor_hash"] = reg.safety.descriptor_hash
                pa["risk_snapshot"] = {
                    "risk_level": reg.safety.risk_level,
                    "requires_confirmation": reg.safety.requires_confirmation,
                    "writes_external_world": reg.safety.writes_external_world,
                    "can_delete": reg.safety.can_delete,
                }
            logger.info("[TRACE:graph] CONFIRM(ns): prepared %d pending approvals", len(_pending_approval_list))
            return {}

        def _tools_node(state: _AgentState) -> dict[str, Any]:
            """tools 节点：执行工具并追踪执行记录。

            Phase 1 接线：
            - 调用前：向 WorkingState.running_tool_state 登记每个 tool_call；
            - 调用后：规范化工具结果（大型结果有界引用化为 Artifact），
              并从 running_tool_state 移除、写入 verified_tool_states、
              若有 artifact 引用则写入 artifact_refs。
            """
            native_tool_node = ToolNode(tools)
            last_msg = state["messages"][-1]
            tc_list = (last_msg.tool_calls or []) if isinstance(last_msg, AIMessage) else []
            tc_names = [tc.get("name", "") for tc in tc_list]
            logger.info("[TRACE:graph] TOOLS(ns): invoking %d tools=%s", len(tc_names), tc_names)

            # ── 调用前：登记 running_tool_state + 未提交副作用 ──
            if ws_service is not None and tc_list:
                ws: WorkingStateService = ws_service
                def _register(db: Session) -> None:
                    for tc in tc_list:
                        ws.add_running_tool(
                            db, thread_id, turn_record_id, execution_id,
                            str(tc.get("id", "") or ""), str(tc.get("name", "") or ""),
                        )
                        # 副作用跟踪：writes_external_world 的工具登记未提交副作用
                        reg = registry.get(str(tc.get("name", "") or "")) if registry is not None else None
                        if reg is not None and reg.safety.writes_external_world:
                            ws.add_uncommitted_side_effect(
                                db, thread_id,
                                ref=str(tc.get("id", "") or ""),
                                description=str(tc.get("name", "") or ""),
                                risk=reg.safety.risk_level,
                            )
                _ws_commit(_register)

            try:
                result = native_tool_node.invoke(state)
                logger.info("[TRACE:graph] TOOLS(ns): result messages=%d", len(result.get("messages", [])))
            except Exception:
                # 调用失败：尽力清理 running_tool_state + 未提交副作用（recover_orphaned_tools 兜底）
                if ws_service is not None and tc_list:
                    ws_b: WorkingStateService = ws_service
                    def _clean(db: Session) -> None:
                        for tc in tc_list:
                            ws_b.remove_running_tool(db, thread_id, str(tc.get("id", "") or ""))
                            ws_b.remove_uncommitted_side_effect(db, thread_id, str(tc.get("id", "") or ""))
                    _ws_commit(_clean)
                logger.exception("[TRACE:graph] TOOLS(ns): invoke FAILED")
                raise

            tool_messages = [m for m in result.get("messages", []) if isinstance(m, ToolMessage)]
            batch_index = max(
                0,
                sum(1 for m in state["messages"] if isinstance(m, AIMessage) and m.tool_calls) - 1,
            )

            for i, tc in enumerate(tc_list):
                tm = tool_messages[i] if i < len(tool_messages) else None
                raw_content = str(tm.content) if tm else ""
                receipt = parse_tool_execution_receipt(raw_content)
                is_error = not receipt.ok
                record_status = receipt.status

                # ── A: 规范化（有界引用化 / 内联）──
                artifact_ref = ""
                if tm is not None and normalizer is not None:
                    norm: ToolResultNormalizer = normalizer
                    try:
                        view = norm.normalize(
                            raw_content, thread_id, trace_id,
                            turn_record_id=turn_record_id, execution_id=execution_id,
                            tool_call_id=str(tc.get("id", "") or ""),
                        )
                        tm.content = view.to_tool_message_content()
                        if view.is_reference and view.reference:
                            artifact_ref = view.reference.get("artifact_ref", "") or ""
                    except Exception:
                        logger.exception("[TRACE:graph] TOOLS(ns): normalize 失败，保留原内容")

                tool_records.append({
                    "name": tc.get("name", ""),
                    "params": args_by_id.get(str(tc.get("id", "") or ""), {}),
                    "result": {
                        "ok": not is_error,
                        "result": str(tm.content) if tm else raw_content,
                        "operation_id": receipt.operation_id,
                        "execution_status": receipt.execution_status,
                        "error_type": receipt.error_type,
                    },
                    "status": record_status,
                    "trace_id": trace_id,
                    "tool_call_id": str(tc.get("id", "") or ""),
                    "batch_index": batch_index,
                })

                # ── B: 调用后维护 verified_tool_states / running_tool_state / artifact_refs ──
                if ws_service is not None:
                    ws_c: WorkingStateService = ws_service
                    def _finalize(
                        db: Session,
                        _tc: Any = tc,
                        _ref: Any = artifact_ref,
                        _err: Any = is_error,
                        _record_status: str = record_status,
                    ) -> None:
                        ws_c.remove_running_tool(db, thread_id, str(_tc.get("id", "") or ""))
                        if _record_status != "execution_unknown":
                            ws_c.remove_uncommitted_side_effect(db, thread_id, str(_tc.get("id", "") or ""))
                            ws_c.update_verified_tool_state(
                                db, thread_id, str(_tc.get("name", "") or ""),
                                {"ok": not _err, "tool_call_id": str(_tc.get("id", "") or "")},
                            )
                        if _ref:
                            ws_c.add_artifact_ref(db, thread_id, str(_ref), "tool_result", str(_tc.get("name", "") or ""))
                    _ws_commit(_finalize)

            return {
                "messages": result.get("messages", []),
                "tool_records": tool_records[-len(tc_list):] if tc_list else [],
            }

        graph = StateGraph(_AgentState)
        graph.add_node("assistant", _assistant)
        graph.add_node("confirm", _confirm_node)
        graph.add_node("validate_tools", _validate_tools)
        graph.add_node("validation_abort", _validation_abort)
        graph.add_node("tools", _tools_node)
        graph.add_edge(START, "assistant")
        graph.add_conditional_edges(
            "assistant",
            _policy_check,
            {"continue": "validate_tools", "confirm": "confirm", "finalize": END},
        )
        graph.add_conditional_edges(
            "validate_tools",
            _route_after_validation,
            {
                "tools": "tools",
                "assistant": "assistant",
                "validation_abort": "validation_abort",
            },
        )
        graph.add_edge("confirm", END)
        graph.add_edge("validation_abort", END)
        graph.add_edge("tools", "assistant")
        compiled = graph.compile()

        return compiled, tool_records, _pending_approval_list

    # ------------------------------------------------------------------
    # Phase 0.5A: _execute_graph（LLM + 工具执行，无 DB 事件写入）
    # ------------------------------------------------------------------

    def _execute_graph(
        self, message: str, exec_ctx: TurnExecutionContext,
        ctx_bundle: Any = None,
        normalizer: "ToolResultNormalizer | None" = None,
    ) -> "AgentGraphResult":
        """Execute graph inference. Uses pre-assembled context from ContextAssembler."""
        thread_id = exec_ctx.thread_id
        turn_id = exec_ctx.turn_id
        turn_record_id = exec_ctx.turn_record_id
        execution_id = exec_ctx.execution_id
        trace_id = exec_ctx.trace_id
        message_source = exec_ctx.message_source
        trace = Trace(trace_id=trace_id) if trace_id else Trace.new()
        thread = self._thread_state.get_or_create_thread(thread_id)

        # Phase 1: 工具结果规范化（有界引用化）+ WorkingState 生命周期维护
        if normalizer is None:
            normalizer = ToolResultNormalizer(LiteLLMTokenCounter(), self._llm_client.default_model)
        ws_service = WorkingStateService()

        # ── Phase 1: ContextAssembler 预组装消息（唯一路径）──
        assembled_ctx = getattr(ctx_bundle, "assembled_ctx", None) if ctx_bundle is not None else None
        if assembled_ctx is None:
            raise RuntimeError("execute_turn 必须通过 ContextAssembler 提供 assembled_ctx")

        chat_messages = assembled_ctx.messages
        tool_schemas = assembled_ctx.tools_schema
        initial_messages = self._dicts_to_langchain_messages(chat_messages)
        context_snapshot = assembled_ctx.snapshot

        # ── Execute graph ──
        langchain_llm = self._build_langchain_llm()
        registry = get_tool_registry()
        run_ctx_exec = RunContext(
            thread_id=thread.id,
            trace_id=trace.trace_id,
            # 执行者即当前会话回合：复用其 MessageSource.value（user /
            # system_command / runtime_event），与 RunContext.source 词汇表兼容。
            source=exec_ctx.message_source.value,
            turn_id=turn_id,
            turn_record_id=turn_record_id,
        )
        tools = build_langchain_tools(registry, run_context=run_ctx_exec)
        # Phase 1: 当 ContextAssembler 已裁剪工具 schema 时，仅绑定该有界子集，
        # 保证 LLM 实际可见工具数与上下文预算一致。
        if tool_schemas is not None:
            allowed = {s.get("function", {}).get("name") for s in tool_schemas}
            tools = [t for t in tools if getattr(t, "name", None) in allowed]
        if not tools:
            logger.warning(
                "[TRACE:graph] 工具列表为空（tool_schemas=%s），LLM 将无工具可用",
                "present" if tool_schemas else "none",
            )
        llm_with_tools = langchain_llm.bind_tools(tools)

        compiled, _records_raw, pending_approvals = self._build_graph(
            llm_with_tools, tools, registry,
            trace_id=trace.trace_id, _thread_id=thread.id,
            _model=self._llm_client.default_model,
            normalizer=normalizer, ws_service=ws_service,
            thread_id=thread.id, turn_record_id=turn_record_id, execution_id=execution_id,
        )
        try:
            result = compiled.invoke({"messages": initial_messages, "tool_records": []})
        except Exception as error:
            logger.exception(
                "[TRACE:graph] compiled.invoke 异常: trace_id=%s model=%s msgs_count=%d tools_count=%d",
                trace.trace_id,
                self._llm_client.default_model,
                len(initial_messages),
                len(tools),
            )
            raise normalize_llm_error(error, trace.trace_id) from error

        # ── 提取回复 ──
        reply = ""
        all_messages = result["messages"]
        llm_usage = self._collect_llm_usage(all_messages)
        for m in reversed(all_messages):
            if isinstance(m, AIMessage) and m.content and not m.tool_calls:
                reply = str(m.content); break
        if not reply:
            for m in reversed(all_messages):
                if isinstance(m, AIMessage) and m.content:
                    reply = str(m.content); break

        # ── ToolRecord（Graph state 是同步与流式共同事实源）──
        tool_records = self._tool_records_from_state(result.get("tool_records", []))
        reply = self._merge_blocked_records(_records_raw, tool_records, reply)

        # 空回复且非审批挂起属于异常情况，记录告警以便定位根因
        # （LLM 空响应、工具绑定异常、streaming 兼容问题等）。
        if not reply and not pending_approvals:
            logger.warning(
                "[TRACE:graph] _execute_graph 返回空回复: trace_id=%s message=%s msgs_count=%d tool_record_count=%d",
                trace.trace_id,
                message[:100],
                len(all_messages),
                len(tool_records),
            )

        # ── 审批记录：CONFIRM 工具未执行，构造 pending_approval 的 ToolRecord ──
        if pending_approvals:
            for pa in pending_approvals:
                tc_name = str(pa.get("name", "") or "")
                tc_args = pa.get("args", {}) if isinstance(pa.get("args"), dict) else {}
                tool_records.append(ToolRecord(
                    tool_call_id=str(pa.get("id", "") or ""),
                    name=tc_name,
                    params=tc_args,
                    result={"ok": False, "result": "awaiting_approval"},
                    status="pending_approval",
                    order_index=len(tool_records),
                ))
            if not reply and pending_approvals:
                tool_names = [str(pa.get("name", "")) for pa in pending_approvals]
                reply = f"需要你的确认来执行以下工具: {', '.join(tool_names)}"

        card_records = [
            ToolCallRecord(name=r.name, params=r.params, result=r.result, status=r.status,
                           trace_id=trace.trace_id, reason=r.reason, tool_call_id=r.tool_call_id)
            for r in tool_records
        ]
        action_cards = build_action_cards(card_records, pending_approvals=pending_approvals)
        pending_operations = build_pending_operations(card_records, pending_approvals=pending_approvals)

        post_items, post_full = self._build_post_context_items(reply, [
            {"name": r.name, "params": r.params, "result": r.result, "status": r.status, "trace_id": trace.trace_id}
            for r in tool_records
        ])
        result_snapshot = ContextSnapshotData(
            items=[*context_snapshot.items, *post_items],
            full_contents={**context_snapshot.full_contents, **post_full},
            stable_prefix_hash=context_snapshot.stable_prefix_hash,
            injected_memory_ids=list(context_snapshot.injected_memory_ids),
        )

        # ── 记忆信号分类（LLM，事务外）──
        signal: MemorySignalDecision = MemorySignalDecision(action=MemorySignalAction.EXTRACT_ASYNC.value, confidence=0.5, reason="default")
        try:
            signal = self._action_planner.classify_memory_signal(user_message=message, reply=reply, trace_id=trace.trace_id)
        except Exception:
            logger.warning("记忆信号分类失败（已使用默认信号）: trace_id=%s", trace.trace_id, exc_info=True)
        signal.action = MemoryExtractionPolicy.resolve_action(
            signal.action, message, message_source,
        ).value

        return AgentGraphResult(
            reply=reply, trace_id=trace.trace_id, user_message=message,
            tool_records=tool_records, action_cards=action_cards,
            pending_operations=pending_operations,
            pending_approvals=[dict(item) for item in pending_approvals],
            context_snapshot=result_snapshot, memory_signal=signal,
            llm_usage=llm_usage,
        )

    # ------------------------------------------------------------------
    # Phase 0.5B: _execute_graph_stream（流式 LLM + 工具执行）
    # ------------------------------------------------------------------

    async def _execute_graph_stream(
        self, message: str, exec_ctx: TurnExecutionContext,
        ctx_bundle: Any = None,
        normalizer: "ToolResultNormalizer | None" = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """流式执行图推理，逐渐产出 token / tool_call / tool_result 事件。

        最后一个事件固定为 {"type": "__graph_result__", "result": AgentGraphResult}。
        利用 LangGraph astream_events(v2) 实现 token 级真正流式传输。
        """
        thread_id = exec_ctx.thread_id
        turn_id = exec_ctx.turn_id
        turn_record_id = exec_ctx.turn_record_id
        execution_id = exec_ctx.execution_id
        trace_id = exec_ctx.trace_id
        message_source = exec_ctx.message_source
        trace = Trace(trace_id=trace_id) if trace_id else Trace.new()
        # 同步 DB 调用不得阻塞事件循环（与 Phase 1 to_thread 先例一致）
        thread = await asyncio.to_thread(self._thread_state.get_or_create_thread, thread_id)

        if normalizer is None:
            normalizer = ToolResultNormalizer(LiteLLMTokenCounter(), self._llm_client.default_model)
        ws_service = WorkingStateService()

        assembled_ctx = getattr(ctx_bundle, "assembled_ctx", None) if ctx_bundle is not None else None
        if assembled_ctx is None:
            raise RuntimeError("execute_turn 必须通过 ContextAssembler 提供 assembled_ctx")

        chat_messages = assembled_ctx.messages
        tool_schemas = assembled_ctx.tools_schema
        initial_messages = self._dicts_to_langchain_messages(chat_messages)
        context_snapshot = assembled_ctx.snapshot

        langchain_llm = self._build_langchain_llm()
        registry = get_tool_registry()
        run_ctx_exec = RunContext(
            thread_id=thread.id,
            trace_id=trace.trace_id,
            # 执行者即当前会话回合：复用其 MessageSource.value（user /
            # system_command / runtime_event），与 RunContext.source 词汇表兼容。
            source=exec_ctx.message_source.value,
            turn_id=turn_id,
            turn_record_id=turn_record_id,
        )
        tools = build_langchain_tools(registry, run_context=run_ctx_exec)
        if tool_schemas is not None:
            allowed = {s.get("function", {}).get("name") for s in tool_schemas}
            tools = [t for t in tools if getattr(t, "name", None) in allowed]
        if not tools:
            logger.warning(
                "[TRACE:graph] 工具列表为空（tool_schemas=%s），LLM 将无工具可用",
                "present" if tool_schemas else "none",
            )
        llm_with_tools = langchain_llm.bind_tools(tools)

        compiled, _records_raw, pending_approvals = self._build_graph(
            llm_with_tools, tools, registry,
            trace_id=trace.trace_id, _thread_id=thread.id,
            _model=self._llm_client.default_model,
            normalizer=normalizer, ws_service=ws_service,
            thread_id=thread.id, turn_record_id=turn_record_id, execution_id=execution_id,
        )

        # ── 流式执行 ──
        accumulated: list[str] = []
        final_state: dict[str, Any] | None = None
        stream_matcher = _StreamToolCallMatcher()
        stream_params_by_id: dict[str, dict[str, Any]] = {}
        completed_stream_records: list[ToolRecord] = []
        input_state: dict[str, Any] = {"messages": initial_messages, "tool_records": []}

        try:
            async for event in compiled.astream_events(input_state, version="v2"):
                kind = event["event"]
                evt_name = event.get("name", "")
                run_id = str(event.get("run_id", "") or "")
                metadata = event.get("metadata", {})
                node = metadata.get("langgraph_node", "")

                if kind == "on_chain_start" and node == "tools":
                    tool_calls = event.get("data", {}).get("input", {}).get("messages", [])
                    if isinstance(tool_calls, list):
                        for stream_message in reversed(tool_calls):
                            if isinstance(stream_message, AIMessage) and stream_message.tool_calls:
                                stream_matcher.register_batch(stream_message.tool_calls)
                                break

                if kind == "on_chain_end" and not event.get("parent_ids"):
                    output = event.get("data", {}).get("output")
                    if isinstance(output, dict) and "messages" in output:
                        final_state = output

                if kind == "on_chat_model_stream":
                    chunk = event["data"].get("chunk")
                    if chunk is not None and chunk.content:
                        text = str(chunk.content)
                        accumulated.append(text)
                        yield {"type": "token", "text": text}

                elif kind == "on_tool_start" and node == "tools":
                    input_data = event["data"].get("input", {})
                    event_tool_call_id = ""
                    if isinstance(input_data, dict):
                        event_tool_call_id = str(
                            input_data.get("tool_call_id", "")
                            or input_data.get("id", "")
                            or ""
                        )
                        tool_params = input_data.get("args", input_data)
                    else:
                        tool_params = {}
                    if not isinstance(tool_params, dict):
                        tool_params = {}
                    tool_params = {
                        key: value for key, value in tool_params.items()
                        if key not in {"tool_call_id", "id"}
                    }
                    tool_call_id = stream_matcher.start(
                        run_id, evt_name, tool_params, event_tool_call_id,
                    )
                    if tool_call_id:
                        stream_params_by_id[tool_call_id] = tool_params
                    yield {
                        "type": "tool_call",
                        "tool_call_id": tool_call_id,
                        "name": evt_name,
                        "params": tool_params,
                    }

                elif kind == "on_tool_end" and node == "tools":
                    output: Any = event["data"].get("output", "")
                    first: Any = None
                    if isinstance(output, dict) and "messages" in output:
                        msgs = output.get("messages", [])
                        first = msgs[0] if msgs else None
                        content = str(first.content) if first and hasattr(first, "content") else str(first) if first else ""
                    elif isinstance(output, (list, tuple)):
                        first = output[0] if output else None
                        content = str(first.content) if first and hasattr(first, "content") else str(first) if first else ""
                    elif hasattr(output, "content"):
                        first = output
                        content = str(getattr(output, "content", ""))
                    else:
                        content = str(output)
                    receipt = parse_tool_execution_receipt(content)
                    result_status = receipt.status
                    # on_tool_end 携带的是规范化前的原始工具输出，可能达数百 KB：
                    # SSE 推送与中断事实持久化均须按内联上限截断（引用化版本
                    # 由 _tools_node 的 normalizer 负责写入正式事实源）。
                    content = _bound_stream_tool_content(content, normalizer)
                    tool_call_id = ""
                    if first is not None and hasattr(first, "tool_call_id"):
                        tool_call_id = str(getattr(first, "tool_call_id", "") or "")
                    tool_call_id = stream_matcher.finish(run_id, tool_call_id)
                    completed_stream_records.append(ToolRecord(
                        tool_call_id=tool_call_id,
                        name=evt_name,
                        params=stream_params_by_id.get(tool_call_id, {}),
                        result={
                            "ok": receipt.ok,
                            "result": content,
                            "operation_id": receipt.operation_id,
                            "execution_status": receipt.execution_status,
                            "error_type": receipt.error_type,
                        },
                        status=result_status,
                        order_index=len(completed_stream_records),
                    ))
                    yield {
                        "type": "tool_result",
                        "tool_call_id": tool_call_id,
                        "name": evt_name,
                        "result": content,
                        "status": result_status,
                    }
        except Exception as error:
            logger.exception(
                "[TRACE:graph] astream_events 异常: trace_id=%s model=%s partial_tools=%d",
                trace.trace_id, self._llm_client.default_model, len(_records_raw),
            )
            normalized = normalize_llm_error(error, trace.trace_id)
            state_records = self._tool_records_from_state(_records_raw)
            known_ids = {record.tool_call_id for record in state_records if record.tool_call_id}
            partial_records = [
                *state_records,
                *[
                    record for record in completed_stream_records
                    if not record.tool_call_id or record.tool_call_id not in known_ids
                ],
            ]
            for index, record in enumerate(partial_records):
                record.order_index = index
            raise GraphExecutionInterrupted(
                error=normalized,
                trace_id=trace.trace_id,
                partial_reply="".join(accumulated),
                tool_records=partial_records,
            ) from error

        reply = "".join(accumulated)
        final_messages: list[Any] = []
        if final_state is not None:
            final_messages = final_state.get("messages", [])
            for final_message in reversed(final_messages):
                if isinstance(final_message, AIMessage) and final_message.content and not final_message.tool_calls:
                    reply = str(final_message.content)
                    break

        # ── Graph state 是正常完成后的唯一事实源 ──
        state_records = final_state.get("tool_records", []) if final_state is not None else _records_raw
        tool_records = self._tool_records_from_state(
            [r for r in state_records if r.get("status") != "blocked"],
        )
        reply = self._merge_blocked_records(_records_raw, tool_records, reply)

        # ── 审批记录 ──
        if pending_approvals:
            for pa in pending_approvals:
                tc_name = str(pa.get("name", "") or "")
                tc_args = pa.get("args", {}) if isinstance(pa.get("args"), dict) else {}
                tool_records.append(ToolRecord(
                    tool_call_id=str(pa.get("id", "") or ""),
                    name=tc_name,
                    params=tc_args,
                    result={"ok": False, "result": "awaiting_approval"},
                    status="pending_approval",
                    order_index=len(tool_records),
                ))
            if not reply and pending_approvals:
                tool_names = [str(pa.get("name", "")) for pa in pending_approvals]
                reply = f"需要你的确认来执行以下工具: {', '.join(tool_names)}"

        if not reply and not pending_approvals and not _records_raw:
            logger.warning(
                "[TRACE:graph] _execute_graph_stream 返回空回复: trace_id=%s message=%s",
                trace.trace_id, message[:100],
            )

        card_records = [
            ToolCallRecord(name=r.name, params=r.params, result=r.result, status=r.status,
                           trace_id=trace.trace_id, reason=r.reason, tool_call_id=r.tool_call_id)
            for r in tool_records
        ]
        action_cards = build_action_cards(card_records, pending_approvals=pending_approvals)
        pending_operations = build_pending_operations(card_records, pending_approvals=pending_approvals)

        post_items, post_full = self._build_post_context_items(reply, [
            {"name": r.name, "params": r.params, "result": r.result, "status": r.status, "trace_id": trace.trace_id}
            for r in tool_records
        ])
        result_snapshot = ContextSnapshotData(
            items=[*context_snapshot.items, *post_items],
            full_contents={**context_snapshot.full_contents, **post_full},
            stable_prefix_hash=context_snapshot.stable_prefix_hash,
            injected_memory_ids=list(context_snapshot.injected_memory_ids),
        )

        signal: MemorySignalDecision = MemorySignalDecision(action=MemorySignalAction.EXTRACT_ASYNC.value, confidence=0.5, reason="default")
        try:
            # 同步 LLM HTTP 调用（最长可达 30s），必须移出事件循环线程
            signal = await asyncio.to_thread(
                self._action_planner.classify_memory_signal,
                user_message=message, reply=reply, trace_id=trace.trace_id,
            )
        except Exception:
            logger.warning("记忆信号分类失败（已使用默认信号）: trace_id=%s", trace.trace_id, exc_info=True)
        signal.action = MemoryExtractionPolicy.resolve_action(
            signal.action, message, message_source,
        ).value

        yield {
            "type": "__graph_result__",
            "result": AgentGraphResult(
                reply=reply, trace_id=trace.trace_id, user_message=message,
                tool_records=tool_records, action_cards=action_cards,
                pending_operations=pending_operations,
                pending_approvals=[dict(item) for item in pending_approvals],
                context_snapshot=result_snapshot, memory_signal=signal,
                llm_usage=self._collect_llm_usage(final_messages),
            ),
        }

    @staticmethod
    def _merge_blocked_records(
        records_raw: list[dict[str, Any]],
        tool_records: list["ToolRecord"],
        reply: str,
    ) -> str:
        """将 policy BLOCK 记录合并进 tool_records，并在无回复时给出如实说明。

        BLOCK 时 tools 节点未执行，被阻止的调用只存在于共享 records_raw 中
        （状态为 blocked），需在此并入结果，产出 tool_blocked 卡片而非
        伪装成 empty_llm_response。返回（可能被替换的）回复文本。
        """
        blocked_raw = [r for r in records_raw if r.get("status") == "blocked"]
        for r in blocked_raw:
            tool_records.append(ToolRecord(
                tool_call_id=str(r.get("tool_call_id", "") or ""),
                batch_index=int(r.get("batch_index", 0) or 0),
                name=str(r.get("name", "") or ""),
                params=r.get("params", {}) if isinstance(r.get("params"), dict) else {},
                result=r.get("result", {"ok": False, "result": "blocked"}),
                status="blocked",
                order_index=len(tool_records),
                reason=str(r.get("reason", "") or ""),
            ))
        if not reply and blocked_raw:
            names = ", ".join(sorted({str(r.get("name", "") or "") for r in blocked_raw}))
            reply = f"工具调用被策略阻止（工具未注册）: {names}。请检查工具配置后重试。"
        return reply

    @staticmethod
    def _tool_records_from_state(records: list[dict[str, Any]]) -> list["ToolRecord"]:
        """将 Graph state 中的工具事实转换为统一 ToolRecord。"""
        return [
            ToolRecord(
                tool_call_id=str(record.get("tool_call_id", "") or ""),
                batch_index=int(record.get("batch_index", 0) or 0),
                name=str(record.get("name", "") or ""),
                params=record.get("params", {}) if isinstance(record.get("params"), dict) else {},
                result=record.get("result", {"ok": False, "result": ""}),
                status=str(record.get("status", "completed") or "completed"),
                order_index=index,
                reason=str(record.get("reason", "") or ""),
            )
            for index, record in enumerate(records)
        ]

    # ------------------------------------------------------------------
    # 历史重建：从结构化事件重建 LangChain 消息序列
    # ------------------------------------------------------------------

    @staticmethod
    def _dicts_to_langchain_messages(chat_messages: list[dict[str, Any]]) -> list[BaseMessage]:
        """Convert assembled chat dicts to LangChain messages."""
        result: list[BaseMessage] = []
        for m in chat_messages:
            role = m.get("role", "")
            content = m.get("content", "")
            tool_calls = m.get("tool_calls")
            if role == "system":
                result.append(SystemMessage(content=content))
            elif role == "user":
                result.append(HumanMessage(content=content))
            elif role == "assistant":
                if tool_calls:
                    tcs = [normalize_tool_call(tc) for tc in tool_calls]
                    result.append(AIMessage(content=content, tool_calls=tcs))
                else:
                    result.append(AIMessage(content=content))
            elif role == "tool":
                normalized = normalize_tool_result(m)
                result.append(ToolMessage(
                    content=content,
                    tool_call_id=normalized["id"],
                    name=normalized["name"],
                ))
        return result

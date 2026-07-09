"""
运行时层 - Agent 主循环（精简编排层）。

所有工具调用由 LLM + bind_tools() 原生机制通过 LangGraph 的 StateGraph +
ToolNode 处理。不存在手动 ReAct 循环、自定义 <tool_call> XML 解析和硬编码的
工具调度或摘要逻辑。

核心职责：
1. 构建 LangChain LLM 并绑定工具
2. 构建包含身份、记忆、任务的可变系统提示
3. 组装对话历史
4. 构建并运行 LangGraph StateGraph
5. 记录事件日志并完成持久化
"""

from __future__ import annotations

import json as _json
import logging
from typing import Annotated, Optional, TypedDict

logger = logging.getLogger(__name__)

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from sqlalchemy.orm import Session

from aiive.core.action_planner import ActionPlanner, AgentDecision
from aiive.core.context_builder import ContextBuilder, ContextItem
from aiive.core.llm_client import LLMClient, LLMResponse
from aiive.db.models import ContextSnapshot, Task
from aiive.memory.memory_store import MemoryStore
from aiive.runtime.event_logger import EventLogger
from aiive.runtime.policy_engine import check_tool_calls, PolicyAction
from aiive.runtime.thread_state import ThreadState
from aiive.runtime.tool_executor import ToolCallRecord, ToolExecutor
from aiive.runtime.trace import Trace
from aiive.tools.langchain_adapter import build_langchain_tools
from aiive.tools.registry import get_tool_registry
from aiive.worker.outbox_handlers import register_all
from aiive.worker.outbox_worker import OutboxWorker


# 模块级 AgentState，必须定义在模块层级，以便 get_type_hints()
# 能从实例方法内部的闭包中解析类型
class _AgentState(TypedDict):
    """LangGraph 状态字典（模块级定义，支持类型解析）。"""
    messages: Annotated[list, add_messages]


def _create_tool_result_events(db: Session, thread_id: str, records: list[dict]) -> None:
    """基于 tool result 在主 DB 会话中创建系统事件。

    工具 handler 使用独立 DB 会话（_db_handler 装饰器），不能直接写入带 FK 约束的
    events 表。此函数在主会话中根据工具执行结果创建事件，避免 FK 违规。
    """
    import json as _json, uuid

    from aiive.db.models import Event

    for r in records:
        if r.get("status") != "completed":
            continue
        name = r.get("name", "")
        raw_result = r.get("result", {}).get("result", "")
        inner = {}
        if isinstance(raw_result, str):
            try:
                inner = _json.loads(raw_result)
            except (ValueError, TypeError):
                continue
        elif isinstance(raw_result, dict):
            inner = raw_result

        if name == "schedule_reminder" and inner.get("reminder_set"):
            event = Event(
                id=str(uuid.uuid4()),
                trace_id=r.get("trace_id", ""),
                thread_id=thread_id,
                event_type="reminder_created",
                payload={
                    "task_id": inner.get("task_id", ""),
                    "status": "pending",
                    "content": inner.get("content", ""),
                    "delay_minutes": inner.get("delay_minutes", 0),
                },
            )
            db.add(event)


def _serialize_context_item(item: ContextItem) -> dict:
    """将 ContextItem 序列化为字典，用于持久化存储。"""
    return {
        "item_id": item.item_id,
        "kind": item.kind,
        "source": item.source,
        "trust_level": item.trust_level,
        "content_preview": item.content_preview,
        "token_estimate": item.token_estimate,
    }


class AgentLoop:
    """Agent 主循环精简编排层，对 LangGraph StateGraph 的封装。

    负责每个对话轮次的完整生命周期：
    上下文构建 → LangGraph 图执行 → 结果持久化 → action_cards 生成。

    支持非流式（run）和流式（run_stream）两种执行模式。

    Attributes:
        _llm_client: AIive LLMClient 实例
        _db: SQLAlchemy 数据库会话
        _logger: 事件日志记录器
        _thread_state: 线程状态管理器
        _ctx_builder: 上下文构建器
        _memory_store: 记忆存储
        _action_planner: 动作规划器
        _outbox: Outbox 工作队列
        _last_ctx_items: 最近一次构建的上下文项列表
        _last_ctx_meta: 最近一次上下文的元数据
        _last_intent: 最近一次意图检测结果
        _last_decision: 最近一次 AgentDecision 决策
    """

    def __init__(self, llm_client: LLMClient, db: Session):
        self._llm_client = llm_client
        self._db = db
        self._logger = EventLogger(db)
        self._thread_state = ThreadState(db)
        self._ctx_builder = ContextBuilder()
        self._memory_store = MemoryStore(db)
        self._action_planner = ActionPlanner(llm_client)
        self._outbox = OutboxWorker(lambda: db)
        register_all(self._outbox)
        self._last_ctx_items: list[ContextItem] = []
        self._last_ctx_meta: dict = {}
        self._last_intent: dict = {}
        self._last_decision: AgentDecision | None = None

    # ------------------------------------------------------------------
    # LangChain LLM 工厂
    # ------------------------------------------------------------------

    def _build_langchain_llm(self) -> ChatOpenAI:
        """使用 AIive LLMClient 配置构建 LangChain ChatOpenAI 实例。

        Returns:
            已配置的 ChatOpenAI 实例（temperature=0，确定性输出）
        """
        return ChatOpenAI(
            model=self._llm_client._default_model,
            api_key=self._llm_client._api_key,
            base_url=self._llm_client._base_url,
            temperature=0,
            timeout=self._llm_client._timeout_seconds,
        )

    # ------------------------------------------------------------------
    # 上下文辅助方法
    # ------------------------------------------------------------------

    def _resolve_memories_for_context(self) -> tuple[list[dict], list[dict]]:
        """解析并去重活跃记忆。

        处理同 memory_key 的多条记忆：保留最新一条，旧的标记为被替代（superseded）。

        Returns:
            (resolved, excluded) 二元组
        """
        all_active = list(self._memory_store.resolve_for_context())
        resolved: list[dict] = []
        excluded: list[dict] = []
        seen_keys: dict[str, dict] = {}
        for mem in all_active:
            key = mem.memory_key
            if key and key in seen_keys:
                older = seen_keys[key]
                excluded.append({
                    "id": older["id"], "content": older["content"],
                    "exclusion_reason": "superseded",
                })
                seen_keys[key] = {"id": mem.id, "content": mem.content, "memory_type": mem.memory_type}
            elif key:
                seen_keys[key] = {"id": mem.id, "content": mem.content, "memory_type": mem.memory_type}
            else:
                resolved.append({"id": mem.id, "content": mem.content, "memory_type": mem.memory_type})
        resolved.extend(seen_keys.values())
        return resolved, excluded

    def _get_runtime_identity(self) -> dict[str, str]:
        """从活跃记忆记录中解析 Agent 和用户的运行时身份。

        Returns:
            包含 agent_display_name、agent_runtime_id、user_display_name 的字典
        """
        identity: dict[str, str] = {}
        for mem in self._memory_store.get_active():
            if mem.memory_key == "agent.display_name":
                identity["agent_display_name"] = mem.content
            elif mem.memory_key == "agent.runtime_id":
                identity["agent_runtime_id"] = mem.content
            elif mem.memory_key == "user.name":
                identity["user_display_name"] = mem.content
        return identity

    def _get_tasks_context(self) -> tuple[list[dict], list[dict]]:
        """查询待处理任务，按活跃/到期分类。

        Returns:
            (active, due) 二元组
        """
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        all_tasks = (
            self._db.query(Task)
            .filter(Task.status.in_(["pending", "triggered"]))
            .order_by(Task.next_check_at.asc().nullslast())
            .all()
        )
        active: list[dict] = []
        due: list[dict] = []
        for t in all_tasks:
            d = {"id": t.id, "task_type": t.task_type, "title": t.title, "status": t.status}
            nca = t.next_check_at
            # 确保时区感知
            if nca is not None and nca.tzinfo is None:
                nca = nca.replace(tzinfo=timezone.utc)
            if nca and nca <= now:
                due.append(d)
            else:
                active.append(d)
        return active, due

    def _get_recent_notifications(self) -> list[dict]:
        """获取最近的 5 条通知/提醒事件。

        Returns:
            包含 id、event_type、title、message、status 的字典列表
        """
        from aiive.db.models import Event
        notifs = (
            self._db.query(Event)
            .filter(Event.event_type.in_(["notification_created", "reminder_created"]))
            .order_by(Event.created_at.desc())
            .limit(5)
            .all()
        )
        return [
            {
                "id": e.id, "event_type": e.event_type,
                "title": e.payload.get("title", e.payload.get("content", "")),
                "message": e.payload.get("message", e.payload.get("content", "")),
                "status": e.payload.get("status", ""),
            }
            for e in notifs
        ]

    # ------------------------------------------------------------------
    # 系统提示构建
    # ------------------------------------------------------------------

    @staticmethod
    def _build_system_block(
        runtime_identity: dict[str, str] | None = None,
        resolved_memories: list[dict] | None = None,
        active_tasks: list[dict] | None = None,
        due_tasks: list[dict] | None = None,
    ) -> str:
        """构建带上下文块的系统提示消息。

        组装规则说明、运行时身份、用户记忆、到期任务和活跃任务，
        作为 LLM 调用的 system message。

        Args:
            runtime_identity: 运行时身份信息
            resolved_memories: 已解析的用户记忆（最多 20 条）
            active_tasks: 活跃任务（最多 10 条）
            due_tasks: 到期任务

        Returns:
            组装好的系统提示字符串
        """
        parts = [
            "You are the user's long-running personal agent.",
            "",
            "## Rules",
            "1. For normal questions and chat, respond directly WITHOUT calling tools.",
            "2. For hypothetical questions, do NOT call any tools.",
            "3. Only call tools when the user explicitly asks you to perform an action.",
            "4. Never claim to have performed an action unless a tool actually succeeded.",
            "5. After tool execution, base your response on the actual tool result.",
            "",
            "## Side Effects",
            "Some tools persist data or modify state. Be transparent about actions and results.",
            "Destructive or high-risk operations require user confirmation.",
            "",
            "## Verified Facts vs Claims",
            "Tool results are VERIFIED FACTS. Your own earlier replies are only CLAIMS.",
        ]

        if runtime_identity:
            identity_lines = ["\n## Runtime Identity"]
            rid = runtime_identity.get("agent_runtime_id", "")
            aname = runtime_identity.get("agent_display_name", "")
            uname = runtime_identity.get("user_display_name", "")
            if rid:
                identity_lines.append(f"- agent_runtime_id: {rid}")
            if aname:
                identity_lines.append(f"- agent_display_name: {aname}")
            if uname:
                identity_lines.append(f"- user_display_name: {uname}")
            if len(identity_lines) > 1:
                parts.extend(identity_lines)

        if resolved_memories:
            parts.append("\n## User Memory (Evidence)")
            for mem in resolved_memories[:20]:
                parts.append(f"- {mem.get('content', '')}")

        if due_tasks:
            parts.append("\n## Due Tasks")
            for t in due_tasks:
                parts.append(f"- [{t.get('id', '')[:8]}] {t.get('title', '')}")

        if active_tasks:
            parts.append("\n## Active Tasks (pending)")
            for t in active_tasks[:10]:
                parts.append(f"- [{t.get('id', '')[:8]}] {t.get('title', '')}")

        return "\n".join(parts)

    @staticmethod
    def _snapshot_context(
        system_content: str | None = None,
        resolved_memories: list[dict] | None = None,
        history: list[dict] | None = None,
        current_message: str = "",
    ) -> tuple[list[ContextItem], dict]:
        """从运行时上下文数据构建 ContextItem 列表和元数据，供 ContextSnapshot 持久化。"""
        items: list[ContextItem] = []
        if system_content:
            items.append(ContextItem(
                item_id="system_prefix", kind="stable_prefix", source="system",
                trust_level="trusted",
                content_preview=system_content[:300],
                token_estimate=max(1, len(system_content) // 4),
            ))
        for mem in (resolved_memories or []):
            c = mem.get("content", "")
            items.append(ContextItem(
                item_id=mem.get("id", "")[:8], kind="evidence_memory",
                source="memory_store", trust_level="trusted",
                content_preview=c[:300],
                token_estimate=max(1, len(c) // 4),
            ))
        for h in (history or []):
            c = h.get("content", "")
            if c:
                items.append(ContextItem(
                    item_id=h.get("role", "unknown")[:8], kind="history_message",
                    source="thread", trust_level="trusted",
                    content_preview=c[:300],
                    token_estimate=max(1, len(c) // 4),
                ))
        if current_message:
            items.append(ContextItem(
                item_id="current_message", kind="user_message", source="thread",
                trust_level="trusted",
                content_preview=current_message[:300],
                token_estimate=max(1, len(current_message) // 4),
            ))
        meta = {
            "total_items": len(items),
            "total_tokens": sum(it.token_estimate for it in items),
        }
        return items, meta

    # ------------------------------------------------------------------
    # 图构建和执行
    # ------------------------------------------------------------------

    def _build_and_run_graph(
        self,
        llm_with_tools,
        tools,
        registry,
        initial_messages: list[BaseMessage],
    ) -> tuple[str, list[dict], list[BaseMessage]]:
        """构建 StateGraph，执行调用，返回 (reply, tool_records, all_messages)。

        图结构:
        START -> assistant -> [policy_check] -> tools -> assistant -> ...
                                                      -> END

        策略检查：如果 policy_check 返回 BLOCK 或 CONFIRM，则直接跳到 END，
        不执行工具节点。

        Args:
            llm_with_tools: 带绑定工具的 LLM
            tools: LangChain 工具列表
            registry: ToolRegistry 实例
            initial_messages: 初始消息列表

        Returns:
            (reply, tool_records, all_messages) 三元组
        """
        def _assistant(state: _AgentState) -> dict:
            """assistant 节点：LLM 推理，可生成 tool_calls。"""
            resp = llm_with_tools.invoke(state["messages"])
            return {"messages": [resp]}

        def _policy_check(state: _AgentState) -> str:
            """策略检查节点：根据 ToolRegistry 元数据校验 tool_calls。

            Returns:
                "continue"（继续执行工具）或 "finalize"（终止并返回结果）
            """
            msgs = state.get("messages", [])
            if not msgs:
                return "finalize"
            last_msg = msgs[-1]
            if not isinstance(last_msg, AIMessage) or not last_msg.tool_calls:
                return "finalize"
            tool_calls_raw = [
                {"name": tc["name"], "args": tc.get("args", {}), "id": tc.get("id", "")}
                for tc in last_msg.tool_calls
            ]
            result = check_tool_calls(tool_calls_raw, registry)
            if result.action in (PolicyAction.BLOCK, PolicyAction.CONFIRM):
                return "finalize"
            return "continue"

        graph = StateGraph(_AgentState)
        graph.add_node("assistant", _assistant)
        graph.add_node("tools", ToolNode(tools))
        graph.add_edge(START, "assistant")
        graph.add_conditional_edges(
            "assistant",
            _policy_check,
            {"continue": "tools", "finalize": END},
        )
        graph.add_edge("tools", "assistant")
        compiled = graph.compile()

        result = compiled.invoke({"messages": initial_messages})

        # 从最后一条不含 tool_calls 的 AIMessage 中提取回复
        reply = ""
        all_messages = result["messages"]
        for m in reversed(all_messages):
            if isinstance(m, AIMessage) and m.content and not m.tool_calls:
                reply = str(m.content)
                break
        if not reply:
            for m in reversed(all_messages):
                if isinstance(m, AIMessage) and m.content:
                    reply = str(m.content)
                    break

        # 提取工具调用记录
        records: list[dict] = []
        for m in all_messages:
            if hasattr(m, "tool_call_id") and hasattr(m, "name"):
                content = str(m.content)
                # 检测 JSON 格式的 Error 响应或纯文本 Error 前缀
                is_error = False
                if content.startswith("Error:"):
                    is_error = True
                elif content.startswith("{"):
                    try:
                        parsed = __import__("json").loads(content)
                        if isinstance(parsed, dict) and not parsed.get("ok", True):
                            is_error = True
                    except (ValueError, TypeError):
                        pass
                records.append({
                    "name": getattr(m, "name", "unknown"),
                    "result": {"ok": not is_error, "result": content},
                    "status": "failed" if is_error else "completed",
                })

        return reply, records, all_messages

    # ------------------------------------------------------------------
    # 主入口：非流式执行
    # ------------------------------------------------------------------

    def run(self, message: str, thread_id: Optional[str] = None) -> dict:
        """执行一次非流式的 Agent 对话轮次。

        完整流程：
        1. 创建 Trace 和 Thread（如需要）
        2. 记录用户消息事件
        3. 构建运行时上下文（记忆、身份、任务）
        4. 组装初始消息（系统提示 + 历史 + 当前消息）
        5. 运行 LangGraph 图
        6. 调用 _finalize 完成持久化和清理

        Args:
            message: 用户输入消息
            thread_id: 会话线程 ID（可选，不传则创建新线程）

        Returns:
            包含 reply、thread_id、trace_id、action_cards、tool_calls 等的字典
        """
        trace = Trace.new()
        thread = self._thread_state.get_or_create_thread(thread_id)

        if not thread_id:
            self._logger.log_event(
                trace_id=trace.trace_id, thread_id=thread.id, event_type="chat_started"
            )
        self._logger.log_event(
            trace_id=trace.trace_id, thread_id=thread.id,
            event_type="user_message", payload={"content": message},
        )

        # 构建上下文
        resolved_memories, excluded_memories = self._resolve_memories_for_context()
        runtime_identity = self._get_runtime_identity()
        active_tasks, due_tasks = self._get_tasks_context()
        history = self._thread_state.get_recent_messages(thread.id)

        # 构建 LangChain LLM 并绑定工具
        langchain_llm = self._build_langchain_llm()
        registry = get_tool_registry()
        tools = build_langchain_tools(registry, thread_id=thread.id)
        llm_with_tools = langchain_llm.bind_tools(tools)

        # 构建初始消息列表
        system_content = self._build_system_block(runtime_identity, resolved_memories, active_tasks, due_tasks)
        self._last_ctx_items, self._last_ctx_meta = self._snapshot_context(
            system_content=system_content,
            resolved_memories=resolved_memories,
            history=history,
            current_message=message,
        )
        initial_messages: list[BaseMessage] = [SystemMessage(content=system_content)]
        for h in history:
            role = h.get("role", "user")
            content = h.get("content", "")
            if not content:
                continue
            if role == "user":
                initial_messages.append(HumanMessage(content=content))
            elif role == "assistant":
                initial_messages.append(AIMessage(content=content))
        initial_messages.append(HumanMessage(content=message))

        # 运行图
        reply, records, _all_msgs = self._build_and_run_graph(
            llm_with_tools, tools, registry, initial_messages,
        )

        # 附加 trace_id 到记录
        for r in records:
            r["trace_id"] = trace.trace_id

        return self._finalize(reply, message, thread, trace, records, [])

    # ------------------------------------------------------------------
    # 主入口：流式执行
    # ------------------------------------------------------------------

    def run_stream(self, message: str, thread_id: Optional[str] = None):
        """执行一次流式的 Agent 对话轮次（生成器）。

        使用 LangGraph stream API，逐步产生 token、tool_call、tool_result 事件，
        最终产生 done 事件。

        Args:
            message: 用户输入消息
            thread_id: 会话线程 ID（可选）

        Yields:
            事件字典，类型包括：token（字符流）、tool_call（工具调用开始）、
            tool_result（工具执行结果）、done（轮次结束）
        """
        trace = Trace.new()
        thread = self._thread_state.get_or_create_thread(thread_id)
        all_records: list[dict] = []

        if not thread_id:
            self._logger.log_event(
                trace_id=trace.trace_id, thread_id=thread.id, event_type="chat_started"
            )
        self._logger.log_event(
            trace_id=trace.trace_id, thread_id=thread.id,
            event_type="user_message", payload={"content": message},
        )

        resolved_memories, excluded_memories = self._resolve_memories_for_context()
        runtime_identity = self._get_runtime_identity()
        active_tasks, due_tasks = self._get_tasks_context()
        history = self._thread_state.get_recent_messages(thread.id)

        langchain_llm = self._build_langchain_llm()
        registry = get_tool_registry()
        tools = build_langchain_tools(registry, thread_id=thread.id)
        llm_with_tools = langchain_llm.bind_tools(tools)

        system_content = self._build_system_block(runtime_identity, resolved_memories, active_tasks, due_tasks)
        self._last_ctx_items, self._last_ctx_meta = self._snapshot_context(
            system_content=system_content,
            resolved_memories=resolved_memories,
            history=history,
            current_message=message,
        )
        initial_messages: list[BaseMessage] = [SystemMessage(content=system_content)]
        for h in history:
            role = h.get("role", "user")
            content = h.get("content", "")
            if not content:
                continue
            if role == "user":
                initial_messages.append(HumanMessage(content=content))
            elif role == "assistant":
                initial_messages.append(AIMessage(content=content))
        initial_messages.append(HumanMessage(content=message))

        # 构建流式图
        def _assistant(state: _AgentState) -> dict:
            """assistant 节点：LLM 推理。"""
            resp = llm_with_tools.invoke(state["messages"])
            return {"messages": [resp]}

        def _policy_check(state: _AgentState) -> str:
            """策略检查节点：返回值路由到 tools 或直接结束。"""
            msgs = state.get("messages", [])
            if not msgs:
                return "finalize"
            last_msg = msgs[-1]
            if not isinstance(last_msg, AIMessage) or not last_msg.tool_calls:
                return "finalize"
            tool_calls_raw = [
                {"name": tc["name"], "args": tc.get("args", {}), "id": tc.get("id", "")}
                for tc in last_msg.tool_calls
            ]
            result = check_tool_calls(tool_calls_raw, registry)
            if result.action in (PolicyAction.BLOCK, PolicyAction.CONFIRM):
                return "finalize"
            return "continue"

        graph = StateGraph(_AgentState)
        graph.add_node("assistant", _assistant)
        graph.add_node("tools", ToolNode(tools))
        graph.add_edge(START, "assistant")
        graph.add_conditional_edges(
            "assistant",
            _policy_check,
            {"continue": "tools", "finalize": END},
        )
        graph.add_edge("tools", "assistant")
        compiled = graph.compile()

        # 流式执行
        accumulated: list[str] = []
        tool_emitted: set = set()

        for event in compiled.stream(
            {"messages": initial_messages},
            stream_mode="updates",
        ):
            for node_name, node_output in event.items():
                if node_name == "assistant" and "messages" in node_output:
                    for msg in node_output["messages"]:
                        if isinstance(msg, AIMessage):
                            # 发送 tool_call 事件（去重）
                            if msg.tool_calls:
                                for tc in msg.tool_calls:
                                    key = f"{tc['name']}_{tc.get('id', '')}"
                                    if key not in tool_emitted:
                                        tool_emitted.add(key)
                                        yield {
                                            "event": "tool_call",
                                            "data": {
                                                "name": tc["name"],
                                                "params": tc.get("args", {}),
                                                "status": "pending",
                                                "trace_id": trace.trace_id,
                                            },
                                        }
                            # 发送 token 事件
                            if msg.content:
                                content = str(msg.content)
                                accumulated.append(content)
                                for char in content:
                                    yield {"event": "token", "data": {"text": char}}

                elif node_name == "tools" and "messages" in node_output:
                    for msg in node_output["messages"]:
                        if hasattr(msg, "tool_call_id"):
                            content = str(msg.content)
                            is_error = content.startswith("Error:")
                            if not is_error and content.startswith("{"):
                                try:
                                    parsed = __import__("json").loads(content)
                                    if isinstance(parsed, dict) and not parsed.get("ok", True):
                                        is_error = True
                                except (ValueError, TypeError):
                                    pass
                            all_records.append({
                                "name": getattr(msg, "name", "unknown"),
                                "result": {"ok": not is_error, "result": content},
                                "status": "failed" if is_error else "completed",
                                "trace_id": trace.trace_id,
                            })
                            yield {
                                "event": "tool_result",
                                "data": {
                                    "name": getattr(msg, "name", "unknown"),
                                    "result": content,
                                    "status": "failed" if is_error else "completed",
                                    "trace_id": trace.trace_id,
                                },
                            }

        reply = "".join(accumulated)
        result = self._finalize(reply, message, thread, trace, all_records, [])
        action_cards = ToolExecutor(self._db, self._logger).build_action_cards(
            [ToolCallRecord(
                name=r["name"], params={}, result=r["result"],
                status=r["status"], trace_id=r["trace_id"],
            ) for r in all_records]
        )
        yield {
            "event": "done",
            "data": {
                "reply": reply,
                "thread_id": result["thread_id"],
                "trace_id": result["trace_id"],
                "action_cards": action_cards,
                "tool_calls": [{"name": r["name"], "params": {}, "status": r["status"]} for r in all_records],
                "tool_results": [{"name": r["name"], "result": r["result"], "status": r["status"]} for r in all_records],
                "parse_errors": [],
            },
        }

    # ------------------------------------------------------------------
    # 构建消息（向后兼容）
    # ------------------------------------------------------------------

    def _build_messages(self, message: str, thread, trace_id: str = "", tool_results: list[dict] | None = None) -> list[dict]:
        """构建完整的 LLM 消息列表（向后兼容方法）。

        包含动作决策、意图检测、任务上下文、通知和工具模式。

        Args:
            message: 用户消息
            thread: Thread 对象
            trace_id: 追踪 ID
            tool_results: 之前的工具调用结果（可选）

        Returns:
            消息字典列表
        """
        resolved_memories, excluded_memories = self._resolve_memories_for_context()
        runtime_identity = self._get_runtime_identity()

        registry = get_tool_registry()
        all_tools = registry.list_all()
        tool_schemas = {
            "tool_schemas_text": registry.render_tool_schemas(),
            "injected_tool_names": [t["capability_id"] for t in all_tools],
        }

        decision = self._action_planner.plan(
            user_message=message,
            runtime_identity=runtime_identity,
            tool_schemas_text=tool_schemas.get("tool_schemas_text", ""),
            trace_id=trace_id,
        )
        self._last_decision = decision
        intent_result = decision.to_intent_dict()
        self._last_intent = intent_result

        active_tasks, due_tasks = self._get_tasks_context()
        recent_notifications = self._get_recent_notifications()
        history = self._thread_state.get_recent_messages(thread.id)

        messages, items, meta = self._ctx_builder.build(
            history=history,
            current_message=message,
            thread_id=thread.id,
            trace_id=trace_id,
            runtime_identity=runtime_identity,
            intent_result=intent_result,
            resolved_memories=resolved_memories,
            excluded_memories=excluded_memories,
            active_tasks=active_tasks,
            due_tasks=due_tasks,
            recent_notifications=recent_notifications,
            active_tool_schemas=tool_schemas,
            tool_results=tool_results,
        )
        self._last_ctx_items = items
        self._last_ctx_meta = meta
        return messages

    # ------------------------------------------------------------------
    # 终结处理
    # ------------------------------------------------------------------

    def _finalize(
        self,
        reply: str,
        message: str,
        thread,
        trace,
        records: list[dict],
        malformed_errors: list[str],
    ) -> dict:
        """终结处理：记录事件、排队 outbox 任务、保存快照、构建最终响应。"""
        # 先构建 action_cards，再记入 llm_response 事件（前端轮询时能拿到）
        tool_executor = ToolExecutor(self._db, self._logger)
        action_cards = tool_executor.build_action_cards(
            [ToolCallRecord(
                name=r["name"], params={}, result=r.get("result", {}),
                status=r.get("status", "unknown"), trace_id=r.get("trace_id", ""),
            ) for r in records]
        )

        self._logger.log_event(
            trace_id=trace.trace_id, thread_id=thread.id,
            event_type="llm_response",
            payload={"content": reply, "action_cards": action_cards},
        )
        self._logger.log_event(
            trace_id=trace.trace_id, thread_id=thread.id,
            event_type="chat_ended",
            payload={
                "tool_calls": len(records),
                "tool_succeeded": sum(1 for r in records if r.get("status") == "completed"),
                "tool_failed": sum(1 for r in records if r.get("status") == "failed"),
                "parse_errors": len(malformed_errors),
            },
        )

        # 基于 tool result 在主会话中创建系统事件（避免跨会话 FK 约束冲突）
        _create_tool_result_events(self._db, thread.id, records)

        # 排队 outbox 异步任务
        self._outbox.enqueue(
            db=self._db,
            job_type="memory_extraction",
            payload={
                "user_message": message, "reply": reply, "thread_id": thread.id,
                "intent_type": self._last_intent.get("intent_type", "chat"),
                "execution_mode": self._last_intent.get("execution_mode", "explain_only"),
                "should_execute": self._last_intent.get("should_execute", False),
            },
            trace_id=trace.trace_id,
        )
        self._outbox.enqueue(
            db=self._db,
            job_type="steward_extraction",
            payload={
                "user_message": message, "reply": reply, "thread_id": thread.id,
                "intent_type": self._last_intent.get("intent_type", "chat"),
                "execution_mode": self._last_intent.get("execution_mode", "explain_only"),
                "should_execute": self._last_intent.get("should_execute", False),
            },
            trace_id=trace.trace_id,
        )

        # 保存上下文快照
        meta = dict(self._last_ctx_meta) if self._last_ctx_meta else {}
        snapshot = ContextSnapshot(
            trace_id=trace.trace_id,
            thread_id=thread.id,
            stable_prefix_hash=self._last_ctx_meta.get("stable_prefix_hash", ""),
            context_items=[_serialize_context_item(it) for it in self._last_ctx_items],
            meta={
                "total_items": meta.get("total_items", 0),
                "total_tokens": meta.get("total_tokens", 0),
                "thread_id": meta.get("thread_id", ""),
                "trace_id": meta.get("trace_id", ""),
                "total_tool_calls": len(records),
            },
        )
        self._db.add(snapshot)
        self._outbox.process_all(self._db, max_jobs=10)
        try:
            from aiive.worker.task_worker import TaskWorker
            TaskWorker(self._db).poll_and_notify()
        except Exception:
            logger.exception("后台任务轮询失败")
        self._db.commit()

        return {
            "reply": reply,
            "thread_id": thread.id,
            "trace_id": trace.trace_id,
            "action_cards": action_cards,
            "intent_type": meta.get("intent_type", "plain_chat"),
            "tool_calls": [{"name": r["name"], "params": {}, "status": r.get("status", "")} for r in records],
            "tool_results": [{"name": r["name"], "result": r.get("result", {}), "status": r.get("status", "")} for r in records if r.get("status") in ("completed", "failed")],
            "parse_errors": malformed_errors,
        }

"""
运行时层 - LangGraph 适配器。

为 AIive 聊天管线提供 invoke_chat() 入口，内部委托给 AgentGraph.run()。
AgentGraph 全面使用 LangGraph 原生 tool_calls + ToolNode 机制。
"""

from typing import Any


def invoke_chat(message: str, thread_id: str | None = None) -> dict[str, Any]:
    """通过 AgentGraph 执行单次聊天轮次。

    由 POST /api/chat 调用。每次请求创建新的 AgentGraph 实例并绑定独立的数据库会话。

    Args:
        message: 用户输入消息
        thread_id: 会话线程 ID（可选，不传则创建新线程）

    Returns:
        包含 reply、thread_id、trace_id、action_cards 等的字典
    """
    from aiive.core.llm_client import default_llm_client
    from aiive.runtime.agent_graph import AgentGraph
    from aiive.db.base import SessionLocal

    db = SessionLocal()
    try:
        client = default_llm_client()
        graph = AgentGraph(client, db)
        return graph.run(message=message, thread_id=thread_id)
    finally:
        db.close()

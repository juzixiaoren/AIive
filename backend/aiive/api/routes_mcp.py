"""
API路由模块：MCP（Model-Context-Protocol）能力发现
- 提供 MCP 候选工具搜索接口
- 提供能力列表查询接口
"""
import logging

from fastapi import APIRouter
from pydantic import BaseModel, Field

from aiive.mcp.discovery import search_mcp_candidates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


class MCPSearchRequest(BaseModel):
    """MCP搜索请求体"""
    goal: str = Field(..., min_length=1)


@router.post("/mcp/search")
def search_mcp(request: MCPSearchRequest):
    """根据目标描述搜索 MCP 候选工具

    Args:
        request: 包含 goal 的搜索请求

    Returns:
        匹配的 MCP 候选工具列表，包含名称、版本、描述等
    """
    try:
        candidates = search_mcp_candidates(request.goal)
        return [
            {
                "name": c.name,
                "source": c.source,
                "version": c.version,
                "description": c.description,
                "transport": c.transport,
                "package_ref": c.package_ref,
                "declared_tools": c.declared_tools,
                "risk_notes": c.risk_notes,
                "definition_trust_level": c.definition_trust_level,
                "descriptor_hash": c.descriptor_hash,
            }
            for c in candidates
        ]
    except Exception:
        logger.exception("MCP候选搜索失败: goal=%s", request.goal)
        raise


@router.get("/capabilities")
def list_capabilities(state: str = "candidate"):
    """获取能力列表，当前阶段返回 MCP 候选作为能力项

    Args:
        state: 能力状态过滤（当前未使用，预留）

    Returns:
        能力列表，每个能力包含 capability_id、名称、状态等
    """
    try:
        all_candidates = search_mcp_candidates("")
        return [
            {
                "capability_id": f"mcp:{c.name}",
                "name": c.name,
                "state": "candidate",
                "definition_trust_level": c.definition_trust_level,
                "description": c.description,
                "declared_tools": c.declared_tools,
                "descriptor_hash": c.descriptor_hash,
            }
            for c in all_candidates
        ]
    except Exception:
        logger.exception("获取能力列表失败")
        raise

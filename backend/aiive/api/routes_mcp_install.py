"""
API路由模块：MCP 安装与冒烟测试
- 提供 MCP 候选工具沙箱安装接口
- 提供能力冒烟测试接口
- 提供已安装能力列表查询接口
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from aiive.db.base import get_db
from aiive.db.models import Capability, MCPInstallRecord
from aiive.mcp.discovery import search_mcp_candidates
from aiive.mcp.installer import install_sandbox, run_smoke
from aiive.mcp.runtime_client import MCPRuntimeClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp")


class InstallRequest(BaseModel):
    """安装请求体"""
    package_ref: str = Field(...)
    version: str = "latest"
    transport: str = "stdio"


class SmokeRequest(BaseModel):
    """冒烟测试请求体"""
    tool_name: str = Field(...)
    params: dict[str, Any] = {}


@router.post("/{candidate_name}/install-sandbox")
def install_to_sandbox(
    candidate_name: str,
    request: InstallRequest,
    db: Session = Depends(get_db),
):
    """将 MCP 候选工具安装到沙箱环境

    Args:
        candidate_name: 候选工具名称
        request: 安装请求，包含 package_ref、version、transport
        db: 数据库会话

    Returns:
        安装结果
    """
    candidates = search_mcp_candidates(candidate_name)
    matched = [c for c in candidates if candidate_name.lower() in c.name.lower()]
    if not matched:
        return {"ok": False, "error": f"No MCP candidate found for: {candidate_name}"}

    candidate = matched[0]
    result = install_sandbox(
        db=db,
        candidate_name=candidate.name,
        package_ref=request.package_ref,
        version=request.version,
        transport=request.transport,
        declared_tools=candidate.declared_tools,
        definition={
            "name": candidate.name,
            "source": candidate.source,
            "description": candidate.description,
            "risk_notes": candidate.risk_notes,
        },
    )
    db.commit()
    return result


@router.post("/{capability_id}/smoke")
def smoke_capability(
    capability_id: str,
    request: SmokeRequest,
    db: Session = Depends(get_db),
):
    """对已安装的能力进行冒烟测试

    通过调用该能力真实声明的工具并检查结果来验证能力是否正常工作。

    Args:
        capability_id: 能力ID
        request: 冒烟测试请求，包含 tool_name 和 params
        db: 数据库会话

    Returns:
        冒烟测试结果
    """
    full_id = f"mcp:{capability_id}"
    cap = (
        db.query(Capability)
        .filter(Capability.capability_id == full_id)
        .first()
    )
    if cap is None:
        return {"ok": False, "error": f"Capability not found: {full_id}"}
    if cap.state != "sandbox":
        return {"ok": False, "error": f"Capability must be in sandbox, current: {cap.state}"}

    # 取该能力声明的工具列表（最近一次安装记录）
    install = (
        db.query(MCPInstallRecord)
        .filter(MCPInstallRecord.capability_id == cap.id)
        .order_by(MCPInstallRecord.created_at.desc())
        .first()
    )
    declared_tools = list(install.declared_tools) if install else []

    # 冒烟目标工具必须确实属于该能力，避免用无关 stub 冒充目标能力
    if request.tool_name not in declared_tools:
        return {
            "ok": False,
            "error": (
                f"tool_name '{request.tool_name}' 不是能力 '{full_id}' "
                f"声明的工具（已声明: {declared_tools}）"
            ),
        }

    client = _build_test_client(declared_tools)
    tool_result = client.call_tool(request.tool_name, request.params)

    result = run_smoke(
        db=db,
        capability_id=full_id,
        smoke_result={
            "ok": tool_result.ok,
            "tool_name": request.tool_name,
            "result_preview": str(tool_result.result)[:200] if tool_result.ok else None,
            "error": tool_result.error,
        },
    )
    db.commit()
    return result


@router.get("/capabilities")
def list_installed_capabilities(db: Session = Depends(get_db)):
    """获取已安装的能力列表

    Args:
        db: 数据库会话

    Returns:
        已安装能力列表，按更新时间降序排列，最多50条
    """
    try:
        caps = (
            db.query(Capability)
            .order_by(Capability.updated_at.desc())
            .limit(50)
            .all()
        )
        return [
            {
                "capability_id": c.capability_id,
                "name": c.name,
                "state": c.state,
                "descriptor_hash": c.descriptor_hash,
                "created_at": c.created_at.isoformat(),
            }
            for c in caps
        ]
    except Exception:
        logger.exception("获取已安装能力列表失败")
        raise


def _build_test_client(declared_tools: list[str]) -> MCPRuntimeClient:
    """构建冒烟测试用的 MCP 运行时客户端

    注册目标能力真实声明的工具，而非无关的本地 stub。

    注意：当前环境没有真实 MCP 执行器，声明的工具以“不可执行”的诚实处理器
    注册。冒烟仅验证工具声明与接线是否正确，绝不会伪造成功结果。

    Args:
        declared_tools: 目标能力声明的工具名称列表

    Returns:
        已注册工具的 MCPRuntimeClient 实例
    """
    client = MCPRuntimeClient()

    def _make_not_executable(tool_name: str):
        def _handler(**_kwargs: Any):
            # 当前环境没有真实 MCP 执行器：抛异常使 call_tool 进入错误分支，
            # 返回 ok=False，冒烟会诚实置为 needs_review，绝不伪造成功。
            raise RuntimeError(
                f"本环境无真实 MCP 执行器，无法真正调用工具 '{tool_name}'"
            )
        return _handler

    for name in declared_tools:
        client.register_tool(name, _make_not_executable(name))

    return client

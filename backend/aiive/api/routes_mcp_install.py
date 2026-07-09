"""
API路由模块：MCP 安装与冒烟测试
- 提供 MCP 候选工具沙箱安装接口
- 提供能力冒烟测试接口
- 提供已安装能力列表查询接口
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from aiive.db.base import get_db
from aiive.db.models import Capability
from aiive.mcp.discovery import search_mcp_candidates
from aiive.mcp.installer import install_sandbox, run_smoke

router = APIRouter(prefix="/api/mcp")


class InstallRequest(BaseModel):
    """安装请求体"""
    package_ref: str = Field(...)
    version: str = "latest"
    transport: str = "stdio"


class SmokeRequest(BaseModel):
    """冒烟测试请求体"""
    tool_name: str = Field(...)
    params: dict = {}


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

    通过模拟调用工具并检查结果来验证能力是否正常工作。

    Args:
        capability_id: 能力ID
        request: 冒烟测试请求，包含 tool_name 和 params
        db: 数据库会话

    Returns:
        冒烟测试结果
    """
    from aiive.mcp.runtime_client import MCPRuntimeClient

    # 构建一个简单的测试客户端，注册待测工具
    client = _build_test_client(request.tool_name)
    tool_result = client.call_tool(request.tool_name, request.params)

    result = run_smoke(
        db=db,
        capability_id=f"mcp:{capability_id}",
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


def _build_test_client(tool_name: str) -> MCPRuntimeClient:
    """构建冒烟测试用的 MCP 运行时客户端

    预注册内置测试工具（echo、list_files）。

    Args:
        tool_name: 待测工具名称

    Returns:
        已注册工具的 MCPRuntimeClient 实例
    """
    from aiive.mcp.runtime_client import MCPRuntimeClient

    client = MCPRuntimeClient()

    # 内置测试工具
    def echo(msg: str = "") -> str:
        return f"echo: {msg}"

    def list_files(path: str = ".") -> list[str]:
        import os
        try:
            return os.listdir(path)[:10]
        except Exception:
            return []

    client.register_tool("echo", echo)
    client.register_tool("list_files", list_files)

    return client

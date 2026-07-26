"""
API路由模块：MCP 安装与冒烟测试
- 提供 MCP 候选工具真实沙箱安装接口（npm install 到 .data/mcp_sandbox/）
- 提供能力冒烟测试接口（真实启动 stdio 子进程 → tools/list → 可选 tools/call）
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
from aiive.mcp.runtime_client import build_launch_spec, get_runtime_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp")


class InstallRequest(BaseModel):
    """安装请求体

    launch_args: 启动 server 时附加的命令行参数（如 filesystem server 的允许
                 目录列表）。仅作为参数传给包自身的 bin 入口，不构成命令。
    env_keys: 运行时需要从服务端宿主环境透传的环境变量名（如 API key 名）。
    """
    package_ref: str = Field(...)
    version: str = "latest"
    transport: str = "stdio"
    launch_args: list[str] = []
    env_keys: list[str] = []


class SmokeRequest(BaseModel):
    """冒烟测试请求体

    tool_name 可选：缺省时只做真实 tools/list 校验（默认冒烟标准）；
    指定时额外对该工具做一次真实 tools/call。
    """
    tool_name: str = ""
    params: dict[str, Any] = {}


@router.post("/{candidate_name:path}/install-sandbox")
def install_to_sandbox(
    candidate_name: str,
    request: InstallRequest,
    db: Session = Depends(get_db),
):
    """将 MCP 候选工具真实安装到沙箱环境（npm install）

    Args:
        candidate_name: 候选工具名称（支持带 / 的完整包名）
        request: 安装请求，包含 package_ref、version、transport 等
        db: 数据库会话

    Returns:
        安装结果（含真实 sandbox_path 与 bin 入口）
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
            "trust_level": candidate.definition_trust_level,
        },
        launch_args=request.launch_args,
        env_keys=request.env_keys or candidate.required_env,
    )
    db.commit()
    return result


@router.post("/{capability_id:path}/smoke")
def smoke_capability(
    capability_id: str,
    request: SmokeRequest,
    db: Session = Depends(get_db),
):
    """对已安装的能力进行真实冒烟测试

    流程：真实启动 stdio 子进程 → initialize 握手 → tools/list →
    校验声明工具与真实工具列表的交集/差异（并更新 tool_list_hash）→
    请求指定 tool_name 时额外做一次真实 tools/call。

    Args:
        capability_id: 能力ID（mcp: 前缀之后的名称，支持带 /）
        request: 冒烟测试请求，tool_name 可选
        db: 数据库会话

    Returns:
        冒烟测试结果（run_smoke 按结果推进状态机）
    """
    full_id = f"mcp:{capability_id}"
    cap = (
        db.query(Capability)
        .filter(Capability.capability_id == full_id)
        .first()
    )
    if cap is None:
        return {"ok": False, "error": f"Capability not found: {full_id}"}
    if cap.state not in ("sandbox", "needs_review"):
        return {
            "ok": False,
            "error": f"Capability must be in sandbox/needs_review, current: {cap.state}",
        }

    # 取该能力声明的工具列表（最近一次安装记录）
    install = (
        db.query(MCPInstallRecord)
        .filter(MCPInstallRecord.capability_id == cap.id)
        .order_by(MCPInstallRecord.created_at.desc())
        .first()
    )
    declared_tools = list(install.declared_tools) if install else []

    smoke_result = _run_real_smoke(cap, declared_tools, request)

    # 冒烟拿到真实工具列表时缓存进 definition，供激活/恢复时复用
    if smoke_result.get("tools"):
        definition = dict(cap.definition or {})
        definition["tools"] = smoke_result["tools"]
        cap.definition = definition
    smoke_record = {k: v for k, v in smoke_result.items() if k != "tools"}

    result = run_smoke(db=db, capability_id=full_id, smoke_result=smoke_record)
    db.commit()
    result["smoke_result"] = smoke_record
    return result


def _run_real_smoke(
    cap: Capability,
    declared_tools: list[str],
    request: SmokeRequest,
) -> dict[str, Any]:
    """执行真实冒烟：启动 server → tools/list →（可选）tools/call。

    绝不伪造成功：任一环节失败都如实返回 ok=False 与错误信息。

    Returns:
        冒烟结果字典（含 real_tools、declared_missing、undeclared_extra；
        tools 键携带完整工具定义，供上层缓存，不写入 smoke_result 记录）
    """
    definition = dict(cap.definition or {})
    launch = definition.get("launch")
    if not isinstance(launch, dict):
        return {
            "ok": False,
            "mode": "tools_list",
            "error": "能力缺少真实安装的启动信息（launch），请先执行 install-sandbox",
        }

    client = get_runtime_client()
    try:
        client.configure(cap.capability_id, build_launch_spec(launch))
        tools = client.list_tools(cap.capability_id)
    except Exception as e:
        logger.warning("MCP 冒烟 tools/list 失败: %s error=%s", cap.capability_id, e)
        return {"ok": False, "mode": "tools_list", "error": str(e)}

    real_names = [t["name"] for t in tools]
    declared_set = set(declared_tools)
    real_set = set(real_names)
    base: dict[str, Any] = {
        "real_tools": real_names,
        "declared_missing": sorted(declared_set - real_set),
        "undeclared_extra": sorted(real_set - declared_set),
        "tools": tools,
    }

    if not request.tool_name:
        # 默认标准：tools/list 成功且至少有一个真实工具即通过
        base.update({
            "ok": len(real_names) > 0,
            "mode": "tools_list",
            "error": None if real_names else "server started but exposes no tools",
        })
        return base

    # 指定了 tool_name：必须是真实工具，做一次真实 tools/call
    if request.tool_name not in real_set:
        base.update({
            "ok": False,
            "mode": "tools_call",
            "tool_name": request.tool_name,
            "error": (
                f"tool_name '{request.tool_name}' 不在 server 真实工具列表中"
                f"（真实工具: {real_names}）"
            ),
        })
        return base

    call_result = client.call_tool(
        cap.capability_id, request.tool_name, request.params,
    )
    base.update({
        "ok": call_result.ok,
        "mode": "tools_call",
        "tool_name": request.tool_name,
        "result_preview": str(call_result.result)[:200] if call_result.ok else None,
        "error": call_result.error,
    })
    return base


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

"""
MCP 安装器：管理 MCP（Model Context Protocol）服务器的沙箱安装和冒烟测试。
负责将 MCP 候选服务器安装到本地沙箱环境，管理其版本和状态。
"""

import hashlib
import json
from typing import Any

from sqlalchemy.orm import Session

from aiive.db.models import Capability, CapabilityVersion, MCPInstallRecord


def _hash(data: str) -> str:
    """计算数据的 SHA256 哈希（取前 16 位）。

    参数:
        data: 要哈希的字符串

    返回:
        16 位十六进制哈希字符串
    """
    return hashlib.sha256(data.encode()).hexdigest()[:16]


def install_sandbox(
    db: Session,
    candidate_name: str,
    package_ref: str,
    version: str,
    transport: str,
    declared_tools: list[str],
    definition: dict[str, Any],
) -> dict[str, Any]:
    """将 MCP 候选服务器安装到沙箱环境。

    流程：
    1. 生成 capability_id（格式：mcp:{candidate_name}）
    2. 计算描述符哈希和工具列表哈希
    3. 检查是否已存在，存在则更新状态，不存在则创建
    4. 创建安装记录和版本记录

    参数:
        db: 数据库会话
        candidate_name: 候选服务器名称
        package_ref: 包引用（如 npm:@modelcontextprotocol/server-filesystem）
        version: 版本号
        transport: 通信传输方式
        declared_tools: 声明的工具列表
        definition: 服务器定义字典（含 name、description）

    返回:
        安装结果字典，包含 capability_id、state、descriptor_hash 等
    """
    capability_id = f"mcp:{candidate_name}"
    descriptor_hash = _hash(json.dumps(definition, sort_keys=True))
    tool_list_hash = _hash(",".join(sorted(declared_tools)))

    # 检查是否已有同名能力
    existing = (
        db.query(Capability)
        .filter(Capability.capability_id == capability_id)
        .first()
    )

    if existing:
        # 已有能力：如果描述符变化，标记为需要重新审查
        if existing.descriptor_hash and existing.descriptor_hash != descriptor_hash:
            existing.state = "needs_review"
            db.flush()
        cap = existing
    else:
        # 新建能力：初始状态为 sandbox
        cap = Capability(
            capability_id=capability_id,
            name=candidate_name,
            state="sandbox",
            descriptor_hash=descriptor_hash,
            definition=definition,
        )
        db.add(cap)
        db.flush()

    # 记录安装信息
    install = MCPInstallRecord(
        capability_id=cap.id,
        server_name=candidate_name,
        package_ref=package_ref,
        version=version,
        transport=transport,
        declared_tools=list(declared_tools),
        sandbox_path=f"/tmp/aiive_sandbox/{candidate_name}",
    )
    db.add(install)

    # 记录版本信息
    cv = CapabilityVersion(
        capability_id=cap.id,
        version=version,
        descriptor_hash=descriptor_hash,
        tool_list_hash=tool_list_hash,
    )
    db.add(cv)
    db.flush()

    return {
        "capability_id": cap.capability_id,
        "state": cap.state,
        "descriptor_hash": descriptor_hash,
        "tool_list_hash": tool_list_hash,
        "installed": True,
    }


def run_smoke(
    db: Session,
    capability_id: str,
    smoke_result: dict[str, Any],
) -> dict[str, Any]:
    """对沙箱中的 MCP 能力运行冒烟测试。

    冒烟测试通过 -> 状态变为 active
    冒烟测试失败 -> 状态变为 needs_review

    参数:
        db: 数据库会话
        capability_id: 能力标识符
        smoke_result: 冒烟测试结果字典（含 ok 字段）

    返回:
        测试结果字典，包含 capability_id、state、smoke_passed
    """
    cap = (
        db.query(Capability)
        .filter(Capability.capability_id == capability_id)
        .first()
    )
    if not cap:
        return {"ok": False, "error": f"Capability not found: {capability_id}"}

    if cap.state != "sandbox":
        return {"ok": False, "error": f"Capability must be in sandbox, current: {cap.state}"}

    # 更新最新版本的冒烟测试结果
    version = (
        db.query(CapabilityVersion)
        .filter(CapabilityVersion.capability_id == cap.id)
        .order_by(CapabilityVersion.created_at.desc())
        .first()
    )
    if version:
        version.smoke_result = smoke_result

    # 根据冒烟测试结果更新能力状态
    if smoke_result.get("ok") is True:
        cap.state = "active"
    else:
        cap.state = "needs_review"

    db.flush()
    return {
        "ok": True,
        "capability_id": cap.capability_id,
        "state": cap.state,
        "smoke_passed": smoke_result.get("ok") is True,
    }

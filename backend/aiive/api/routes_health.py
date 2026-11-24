"""
API路由模块：健康检查
- 提供服务健康检查端点，用于探测服务是否正常运行
"""
from typing import Any

from fastapi import APIRouter

from aiive.config import settings

router = APIRouter()


@router.get("/health")
async def health_check(runtime: bool = False):
    """健康检查接口

    返回服务名称和版本信息，用于确认服务是否正常运行。

    Returns:
        包含 ok、service、version 的 JSON 响应
    """
    response: dict[str, Any] = {
        "ok": True,
        "service": settings.app_name,
        "version": settings.app_version,
    }

    if runtime:
        from aiive.project_paths import runtime_identity

        actual = runtime_identity()
        response["runtime"] = {key: actual[key] for key in (
            "pid", "parent_pid", "loaded_slot", "system_contract_module", "stable_contract_audit_ref", "prompt_catalog_sha256",
        )}
    return response

"""
API路由模块：健康检查
- 提供服务健康检查端点，用于探测服务是否正常运行
"""
from fastapi import APIRouter

from aiive.config import settings

router = APIRouter()


@router.get("/health")
async def health_check():
    """健康检查接口

    返回服务名称和版本信息，用于确认服务是否正常运行。

    Returns:
        包含 ok、service、version 的 JSON 响应
    """
    return {
        "ok": True,
        "service": settings.app_name,
        "version": settings.app_version,
    }

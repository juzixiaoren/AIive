"""全局认证守卫：HTTP 中间件 + WebSocket 首帧认证。

边界约定：
- 只有初始化状态、首次注册、登录、健康检查允许匿名；
- 其余全部 HTTP 路由（含 /docs、/openapi.json、隐藏的开发者诊断接口）默认要求
  Authorization: Bearer <token>，未认证 401；
- 操作权限与审批仍由 TaskPolicyEngine / 工具审批决定，认证不替代授权（403 由业务层返回）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from fastapi import Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from aiive.auth.security import AuthError, bearer_token, decode_access_token
from aiive.config import settings

logger = logging.getLogger(__name__)

# 明确白名单：初始化状态、首次注册、登录、健康检查。
EXEMPT_PATHS = frozenset(
    {
        "/health",
        "/api/auth/status",
        "/api/auth/register",
        "/api/auth/login",
    }
)

WS_AUTH_TIMEOUT_SECONDS = 10.0
ANONYMOUS_USER = "anonymous"


def _unauthorized(code: str) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"detail": code},
        headers={"WWW-Authenticate": "Bearer"},
    )


async def auth_guard(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """HTTP 认证中间件：白名单之外一律要求有效 Token。"""
    if (
        not settings.aiive_auth_enabled
        or request.method == "OPTIONS"
        or request.url.path in EXEMPT_PATHS
    ):
        return await call_next(request)

    token = bearer_token(request.headers.get("authorization"))
    if token is None:
        return _unauthorized("not_authenticated")
    try:
        claims = decode_access_token(token)
    except AuthError as error:
        return _unauthorized(error.code)
    request.state.username = claims["sub"]
    return await call_next(request)


async def _close_quietly(websocket: WebSocket, reason: str) -> None:
    try:
        await websocket.close(code=1008, reason=reason)
    except RuntimeError:
        # 连接已断开时 close 会抛 RuntimeError，认证失败结论不变。
        pass


async def authenticate_websocket(
    websocket: WebSocket, *, timeout: float = WS_AUTH_TIMEOUT_SECONDS
) -> str | None:
    """在已 accept 的连接上完成首帧认证。

    客户端必须先发送 {"type": "auth", "access_token": "..."}；
    服务端校验签名与过期时间，成功回 {"type": "auth_ok"} 后业务消息才允许流动。
    失败或超时直接关闭连接，返回 None（调用方必须立即 return，不得发送任何业务数据）。
    """
    if not settings.aiive_auth_enabled:
        return ANONYMOUS_USER
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=timeout)
    except (TimeoutError, asyncio.TimeoutError):
        await _close_quietly(websocket, "auth timeout")
        return None
    except WebSocketDisconnect:
        return None

    try:
        envelope = json.loads(raw)
    except (TypeError, ValueError):
        await _close_quietly(websocket, "auth required")
        return None
    if not isinstance(envelope, dict) or envelope.get("type") != "auth":
        await _close_quietly(websocket, "auth required")
        return None

    token = envelope.get("access_token")
    try:
        claims = decode_access_token(str(token or ""))
    except AuthError:
        await _close_quietly(websocket, "unauthorized")
        return None

    username = claims["sub"]
    await websocket.send_text(
        json.dumps({"type": "auth_ok", "data": {"username": username}}, ensure_ascii=False)
    )
    return username

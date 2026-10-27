"""认证路由：初始化状态、首次注册、登录、当前账号。

路径与既有 API 保持同一前缀风格（/api/...）；除 status/register/login 外，
所有接口都在全局认证守卫之后，必须携带 Authorization: Bearer <token>。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from aiive.auth import service
from aiive.auth.security import AuthError, bearer_token, decode_access_token
from aiive.db.base import get_db

router = APIRouter()


class Credentials(BaseModel):
    """注册与登录共用的凭据体。"""

    username: str
    password: str


def _http_error(error: AuthError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.code)


@router.get("/api/auth/status")
def auth_status(db: Session = Depends(get_db)) -> dict[str, bool]:
    """公开接口：只回答是否已初始化，不泄露账号、密码或 Token。"""
    return service.status(db)


@router.post("/api/auth/register")
def auth_register(
    payload: Credentials, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """仅在尚未初始化时创建唯一所有者账号，成功后直接返回 Token。"""
    try:
        return service.register(db, payload.username, payload.password)
    except AuthError as error:
        raise _http_error(error) from error


@router.post("/api/auth/login")
def auth_login(
    payload: Credentials, request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """账号密码登录；错误密码与不存在账号返回同一错误码。"""
    client = request.client.host if request.client is not None else ""
    try:
        return service.login(db, payload.username, payload.password, client=client)
    except AuthError as error:
        raise _http_error(error) from error


@router.get("/api/auth/me")
def auth_me(request: Request, db: Session = Depends(get_db)) -> dict[str, str]:
    """返回当前账号最小信息，用于前端刷新后恢复登录状态。"""
    username = getattr(request.state, "username", None)
    if not username:
        # 认证关闭（本地调试）时仍允许用显式 Token 自证身份。
        token = bearer_token(request.headers.get("authorization"))
        if token is None:
            raise HTTPException(status_code=401, detail="not_authenticated")
        try:
            username = decode_access_token(token)["sub"]
        except AuthError as error:
            raise _http_error(error) from error
    try:
        return service.me(db, username)
    except AuthError as error:
        raise _http_error(error) from error

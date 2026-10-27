"""单用户账号业务：初始化状态、首次注册、登录、当前账号。

约束（个人场景，刻意保持最小）：
- 全库只允许一个所有者账号，由 auth_owners.singleton 唯一约束在数据库层保证；
- 没有邮箱验证、邀请码、多用户、角色或会话表；
- 登录失败按 (客户端, 用户名) 做进程内滑动窗口限流，避免无节制尝试密码。
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aiive.auth.security import (
    AuthError,
    create_access_token,
    hash_password,
    verify_password,
)
from aiive.config import settings
from aiive.db.models import AuthOwner

USERNAME_MAX_LENGTH = 64
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 256

# key -> 最近失败时间戳（monotonic 秒）。进程内状态，重启即清空，符合单机个人场景。
_login_failures: dict[str, list[float]] = {}


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """用于"账号不存在"分支的等价耗时校验，避免用响应时间探测账号是否存在。"""
    return hash_password("aiive-placeholder-password")


def _normalize_username(username: str) -> str:
    name = (username or "").strip()
    if not name or len(name) > USERNAME_MAX_LENGTH:
        raise AuthError("invalid_username", 422)
    return name


def _validate_password(password: str) -> None:
    if not PASSWORD_MIN_LENGTH <= len(password or "") <= PASSWORD_MAX_LENGTH:
        raise AuthError("invalid_password", 422)


def _session_payload(username: str) -> dict[str, Any]:
    token, expires_at = create_access_token(username)
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_at": expires_at.isoformat(),
        "user": {"username": username},
    }


def status(db: Session) -> dict[str, bool]:
    """是否已完成初始化（存在所有者账号）。不返回任何账号信息。"""
    return {"initialized": db.query(AuthOwner.id).first() is not None}


def register(db: Session, username: str, password: str) -> dict[str, Any]:
    """创建唯一所有者账号；成功后直接返回 Token 完成登录。"""
    name = _normalize_username(username)
    _validate_password(password)
    if db.query(AuthOwner.id).first() is not None:
        raise AuthError("account_already_initialized", 409)
    owner = AuthOwner(username=name, password_hash=hash_password(password))
    db.add(owner)
    try:
        db.commit()
    except IntegrityError as error:
        # 并发注册时唯一约束是最终裁决者：后到者按"已初始化"处理。
        db.rollback()
        raise AuthError("account_already_initialized", 409) from error
    return _session_payload(name)


def _recent_failures(key: str, now: float) -> list[float]:
    window = settings.aiive_auth_login_window_seconds
    stamps = [stamp for stamp in _login_failures.get(key, []) if now - stamp < window]
    _login_failures[key] = stamps
    return stamps


def login(db: Session, username: str, password: str, client: str = "") -> dict[str, Any]:
    """校验用户名密码并签发 Token；失败一律返回同一错误码，不泄露账号是否存在。"""
    name = (username or "").strip()
    key = f"{client}:{name.lower()}"
    now = time.monotonic()
    if len(_recent_failures(key, now)) >= settings.aiive_auth_login_max_attempts:
        raise AuthError("too_many_attempts", 429)

    owner = db.query(AuthOwner).filter(AuthOwner.username == name).first()
    if owner is None:
        verify_password(_dummy_hash(), password)
        _login_failures.setdefault(key, []).append(now)
        raise AuthError("invalid_credentials", 401)
    if not verify_password(owner.password_hash, password):
        _login_failures.setdefault(key, []).append(now)
        raise AuthError("invalid_credentials", 401)

    _login_failures.pop(key, None)
    owner.last_login_at = datetime.now(UTC)
    db.commit()
    return _session_payload(owner.username)


def me(db: Session, username: str) -> dict[str, Any]:
    """返回当前账号的最小信息；账号已不存在时按 Token 失效处理。"""
    owner = db.query(AuthOwner).filter(AuthOwner.username == username).first()
    if owner is None:
        raise AuthError("invalid_token", 401)
    return {"username": owner.username}


def reset_login_failures() -> None:
    """清空限流状态（测试与运维用）。"""
    _login_failures.clear()

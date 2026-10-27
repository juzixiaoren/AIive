"""认证原语：Argon2id 密码哈希、HS256 JWT、持久签名密钥。

设计取舍（个人单用户场景）：
- 只有一条 access_token，没有 Refresh Token / 轮换 / 服务端会话表；
- 登出仅清除客户端 Token，泄露的 Token 在过期前仍然有效；
- 密钥来自配置；未配置时自动生成一次并落盘，保证进程重启后旧 Token 依然有效。
"""
from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from aiive.config import settings

ALGORITHM = "HS256"
ISSUER = "aiive"

# Argon2id 是 PasswordHasher 的默认类型；参数取 argon2-cffi 的推荐默认值。
_hasher = PasswordHasher()

# 已解析的密钥缓存：键包含配置值，测试改配置后能重新解析，不会串味。
_secret_cache: dict[tuple[str, str], str] = {}


class AuthError(Exception):
    """认证业务错误，路由层映射为固定 HTTP 状态码与错误码。"""

    code: str
    status_code: int

    def __init__(self, code: str, status_code: int = 401) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def signing_secret() -> str:
    """返回 HS256 签名密钥；未配置时生成一次并持久化到文件。"""
    configured = settings.aiive_auth_secret.strip()
    cache_key = (configured, settings.aiive_auth_secret_file)
    cached = _secret_cache.get(cache_key)
    if cached:
        return cached
    if configured:
        _secret_cache[cache_key] = configured
        return configured

    path = Path(settings.aiive_auth_secret_file)
    if path.exists():
        stored = path.read_text(encoding="utf-8").strip()
        if stored:
            _secret_cache[cache_key] = stored
            return stored

    generated = secrets.token_urlsafe(48)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(generated, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Windows 上 chmod 语义有限；失败不影响密钥可用性。
        pass
    _secret_cache[cache_key] = generated
    return generated


def hash_password(password: str) -> str:
    """生成 Argon2id 密码哈希（含随机 salt 与参数），绝不保存明文。"""
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """校验密码；哈希损坏或格式非法一律按校验失败处理。"""
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def create_access_token(username: str) -> tuple[str, datetime]:
    """签发长期 access_token，返回 (token, 过期时间)。"""
    now = datetime.now(UTC)
    expires_at = now + timedelta(days=settings.aiive_auth_token_days)
    payload: dict[str, Any] = {
        "sub": username,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "iss": ISSUER,
    }
    token = jwt.encode(payload, signing_secret(), algorithm=ALGORITHM)
    return token, expires_at


def decode_access_token(token: str) -> dict[str, Any]:
    """校验签名、算法与过期时间；任何异常都转成 AuthError。"""
    try:
        claims = jwt.decode(
            token,
            signing_secret(),
            algorithms=[ALGORITHM],
            issuer=ISSUER,
            options={"require": ["sub", "iat", "exp"]},
        )
    except jwt.ExpiredSignatureError as error:
        raise AuthError("token_expired") from error
    except jwt.InvalidTokenError as error:
        raise AuthError("invalid_token") from error
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise AuthError("invalid_token")
    return claims


def bearer_token(authorization: str | None) -> str | None:
    """从 Authorization 头提取 Bearer Token；格式不符返回 None。"""
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()

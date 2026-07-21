"""本地开发者诊断接口的统一访问守卫与响应脱敏。"""
import ipaddress
import re
from typing import Any

from fastapi import HTTPException, Request, status

from aiive.config import settings

_REDACTED = "[已脱敏]"
_SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "set_cookie",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "password",
    "passwd",
    "secret",
    "client_secret",
    "prompt",
    "content",
    "message",
    "input",
    "output",
    "query",
    "source_message",
    "content_preview",
    "full_content",
}
_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*)[^\s,;]+"),
)


def _is_loopback(value: str) -> bool:
    """判断请求地址或 Host 是否仅指向本机 loopback。"""
    candidate = value.strip().lower().rstrip(".")
    if candidate == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def _host_name(host_header: str) -> str:
    """从 Host header 提取不含端口的主机名。"""
    value = host_header.strip()
    if value.startswith("["):
        closing = value.find("]")
        return value[1:closing] if closing >= 0 else ""
    if value.count(":") == 1:
        return value.rsplit(":", 1)[0]
    return value


def require_local_developer(request: Request) -> None:
    """要求诊断开关已启用，且客户端与 Host 均为 loopback。"""
    if not settings.aiive_developer_diagnostics_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="开发者诊断接口未启用",
        )
    client_host = request.client.host if request.client is not None else ""
    host_header = request.headers.get("host", "")
    if not _is_loopback(client_host) or not _is_loopback(_host_name(host_header)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="开发者诊断接口仅允许本机访问",
        )


def redact_diagnostic_text(value: str | None) -> str | None:
    """对诊断文本中的常见凭据进行服务端遮蔽。"""
    if value is None:
        return None
    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(lambda match: f"{match.group(1)}{_REDACTED}", redacted)
    return redacted


def redact_diagnostic_payload(value: Any, key: str | None = None) -> Any:
    """递归遮蔽诊断 payload 中的敏感字段与文本凭据。"""
    normalized_key = key.lower().replace("-", "_") if key else None
    if normalized_key in _SENSITIVE_KEYS:
        return _REDACTED
    if isinstance(value, dict):
        return {
            str(item_key): redact_diagnostic_payload(item_value, str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [redact_diagnostic_payload(item) for item in value]
    if isinstance(value, tuple):
        return [redact_diagnostic_payload(item) for item in value]
    if isinstance(value, str):
        return redact_diagnostic_text(value)
    return value

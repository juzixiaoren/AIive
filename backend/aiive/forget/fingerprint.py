"""HMAC-SHA256 内容指纹计算。

普通 SHA-256 的 value_hash 熵低、可能被字典反推，不得作为不可恢复 tombstone 指纹。
使用 HMAC-SHA256(secret, canonical_value) 代替，密钥轮转时 bump key_version。
"""

import hashlib
import hmac

# 当前密钥版本（配置注入时覆盖）
_current_secret: bytes | None = None
_current_key_version: int = 1


def configure_hmac_secret(secret: str, key_version: int = 1) -> None:
    """注入 HMAC 密钥与版本号（启动时调用一次）。

    secret: 原始密钥字符串
    key_version: 密钥版本号，轮转时递增
    """
    global _current_secret, _current_key_version
    _current_secret = secret.encode("utf-8")
    _current_key_version = key_version


def compute_value_fingerprint(canonical_value: str) -> tuple[str, int]:
    """计算内容指纹。

    Args:
        canonical_value: 规范化后的用户内容（如 canonical_key + 归一化 value）

    Returns:
        (hex_fingerprint, key_version)
    """
    if _current_secret is None:
        raise RuntimeError("HMAC secret not configured; call configure_hmac_secret() first")
    digest = hmac.new(_current_secret, canonical_value.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest(), _current_key_version


def verify_fingerprint(canonical_value: str, fingerprint: str, key_version: int) -> bool:
    """验证指纹是否匹配（为防止时序攻击使用 hmac.compare_digest）。

    若 key_version 与当前密钥版本不一致，视为验证失败（旧密钥轮转后失效）。
    """
    if _current_secret is None:
        raise RuntimeError("HMAC secret not configured")
    if key_version != _current_key_version:
        return False
    expected, _ = compute_value_fingerprint(canonical_value)
    return hmac.compare_digest(expected, fingerprint)

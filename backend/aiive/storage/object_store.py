"""
本地文件系统对象存储适配器。

对象键映射到项目内的持久目录；内容寻址写入具备幂等性，并通过同目录原子替换
避免读取到部分内容。接口保持 bucket/key 形式，便于后续替换为远端对象存储。
"""

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3] / ".data" / "object_store"


@dataclass
class ObjectRef:
    """对象引用，包含存储桶、对象键和可选元数据。"""

    bucket: str
    key: str
    metadata: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.metadata is None:
            self.metadata = {}
        if not self.bucket or Path(self.bucket).is_absolute() or ".." in Path(self.bucket).parts:
            raise ValueError("对象存储桶名称无效")
        if not self.key or Path(self.key).is_absolute() or ".." in Path(self.key).parts:
            raise ValueError("对象键无效")

    @property
    def path(self) -> Path:
        """计算对象在本地适配器中的绝对路径。"""
        return ROOT / self.bucket / self.key


def _atomic_put(ref: ObjectRef, data: bytes) -> ObjectRef:
    """在对象目录内原子写入字节；已存在且内容一致时直接复用。"""
    ref.path.parent.mkdir(parents=True, exist_ok=True)
    if ref.path.exists():
        if ref.path.read_bytes() != data:
            raise ValueError("对象键已存在但内容不一致")
        return ref

    temporary = ref.path.parent / f".{ref.path.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, ref.path)
    finally:
        if temporary.exists():
            from aiive.tools.safe_delete import safe_delete

            safe_delete(str(temporary), "object_store", "trash")
    return ref


def put_text(
    bucket: str,
    key: str,
    text: str,
    metadata: dict[str, Any] | None = None,
) -> ObjectRef:
    """以 UTF-8 字节存储文本对象。"""
    return put_bytes(bucket, key, text.encode("utf-8"), metadata)


def put_bytes(
    bucket: str,
    key: str,
    data: bytes,
    metadata: dict[str, Any] | None = None,
) -> ObjectRef:
    """原子且幂等地存储二进制对象。"""
    return _atomic_put(ObjectRef(bucket=bucket, key=key, metadata=metadata or {}), data)


def put_content_addressed(
    bucket: str,
    data: bytes,
    metadata: dict[str, Any] | None = None,
) -> tuple[ObjectRef, str]:
    """按 SHA-256 内容哈希存储对象并返回引用与哈希。"""
    content_hash = hashlib.sha256(data).hexdigest()
    key = f"sha256/{content_hash[:2]}/{content_hash}"
    ref = put_bytes(bucket, key, data, metadata)
    return ref, content_hash


def get(ref: ObjectRef) -> bytes:
    """读取对象的原始字节。"""
    return ref.path.read_bytes()


def get_verified(ref: ObjectRef, expected_hash: str) -> bytes:
    """读取对象并校验 SHA-256 内容哈希。"""
    data = get(ref)
    actual_hash = hashlib.sha256(data).hexdigest()
    if actual_hash != expected_hash:
        raise ValueError("知识原文对象内容哈希校验失败")
    return data


def get_text(ref: ObjectRef) -> str:
    """以 UTF-8 读取文本对象。"""
    return get(ref).decode("utf-8")


def exists(ref: ObjectRef) -> bool:
    """检查对象是否存在。"""
    return ref.path.is_file()


def delete(ref: ObjectRef) -> None:
    """通过 safe_delete 将对象移入回收站。"""
    from aiive.tools.safe_delete import safe_delete

    decision = safe_delete(str(ref.path), "object_store", "trash")
    if not decision.allowed:
        raise RuntimeError(f"对象安全删除失败：{decision.reason}")

"""
本地文件系统对象存储适配器。

提供兼容 S3/MinIO 接口的对象存储抽象层，使用本地文件系统实现。
bucket/key 模型映射到本地目录结构，方便将来替换为云存储。
"""

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# 对象存储根目录
ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent / ".data" / "object_store"


@dataclass
class ObjectRef:
    """对象引用，包含 bucket、key 和元数据。"""
    bucket: str  # 存储桶名称
    key: str  # 对象键名
    metadata: dict = None  # 自定义元数据

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}

    @property
    def path(self) -> Path:
        """根据 bucket 和 key 计算本地文件路径。"""
        return ROOT / self.bucket / self.key


def put_text(bucket: str, key: str, text: str, metadata: dict | None = None) -> ObjectRef:
    """
    存储文本内容为对象。

    参数:
        bucket: 存储桶名称。
        key: 对象键名。
        text: 文本内容。
        metadata: 自定义元数据字典。

    返回:
        创建的 ObjectRef 引用。
    """
    ref = ObjectRef(bucket=bucket, key=key, metadata=metadata or {})
    ref.path.parent.mkdir(parents=True, exist_ok=True)
    ref.path.write_text(text, encoding="utf-8")
    return ref


def put_bytes(bucket: str, key: str, data: bytes, metadata: dict | None = None) -> ObjectRef:
    """
    存储二进制内容为对象。

    参数:
        bucket: 存储桶名称。
        key: 对象键名。
        data: 二进制数据。
        metadata: 自定义元数据字典。

    返回:
        创建的 ObjectRef 引用。
    """
    ref = ObjectRef(bucket=bucket, key=key, metadata=metadata or {})
    ref.path.parent.mkdir(parents=True, exist_ok=True)
    ref.path.write_bytes(data)
    return ref


def get(ref: ObjectRef) -> bytes:
    """
    读取对象为二进制数据。

    参数:
        ref: ObjectRef 对象引用。

    返回:
        对象的二进制内容。
    """
    return ref.path.read_bytes()


def get_text(ref: ObjectRef) -> str:
    """
    读取对象为文本。

    参数:
        ref: ObjectRef 对象引用。

    返回:
        对象的 UTF-8 文本内容。
    """
    return ref.path.read_text(encoding="utf-8")


def exists(ref: ObjectRef) -> bool:
    """
    检查对象是否存在。

    参数:
        ref: ObjectRef 对象引用。

    返回:
        对象文件是否存在。
    """
    return ref.path.exists()


def delete(ref: ObjectRef):
    """
    安全删除对象，使用 safe_delete 确保可审计。

    参数:
        ref: ObjectRef 对象引用。
    """
    from aiive.tools.safe_delete import safe_delete
    safe_delete(str(ref.path), "test_artifacts", "trash")

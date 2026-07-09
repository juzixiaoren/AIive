"""Local filesystem object store adapter. Compatible with S3/MinIO interface for future swap."""

import shutil
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent / ".data" / "object_store"


@dataclass
class ObjectRef:
    bucket: str
    key: str
    metadata: dict = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}

    @property
    def path(self) -> Path:
        return ROOT / self.bucket / self.key


def put_text(bucket: str, key: str, text: str, metadata: dict | None = None) -> ObjectRef:
    ref = ObjectRef(bucket=bucket, key=key, metadata=metadata or {})
    ref.path.parent.mkdir(parents=True, exist_ok=True)
    ref.path.write_text(text, encoding="utf-8")
    return ref


def put_bytes(bucket: str, key: str, data: bytes, metadata: dict | None = None) -> ObjectRef:
    ref = ObjectRef(bucket=bucket, key=key, metadata=metadata or {})
    ref.path.parent.mkdir(parents=True, exist_ok=True)
    ref.path.write_bytes(data)
    return ref


def get(ref: ObjectRef) -> bytes:
    return ref.path.read_bytes()


def get_text(ref: ObjectRef) -> str:
    return ref.path.read_text(encoding="utf-8")


def exists(ref: ObjectRef) -> bool:
    return ref.path.exists()


def delete(ref: ObjectRef):
    from aiive.tools.safe_delete import safe_delete
    safe_delete(str(ref.path), "test_artifacts", "trash")

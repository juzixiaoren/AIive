"""Desktop Node v2 协议和服务端权威 capability 定义。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


DESKTOP_PROTOCOL_VERSION = 2


@dataclass(frozen=True)
class DesktopCapabilityDefinition:
    description: str = ""
    input_schema: dict[str, Any] | None = None
    risk_level: str = "low"
    requires_confirmation: bool = False
    writes_external_world: bool = False
    can_delete: bool = False
    can_access_secret: bool = False
    effect_mode: str = "externally_reconcilable"
    resource_kind: str = "filesystem"
    timeout_seconds: float = 35


def _object(
    properties: dict[str, Any] | None = None, required: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties or {},
        "required": list(required),
        "additionalProperties": False,
    }


_PATH = {"type": "string", "minLength": 1}
_HASH = {"type": "string"}


# Node 只声明“支持哪些能力及参数 schema”；安全属性由服务器决定。
DESKTOP_CAPABILITIES: dict[str, DesktopCapabilityDefinition] = {
    "desktop_system_info": DesktopCapabilityDefinition(
        description="读取目标电脑的系统信息。", input_schema=_object(), resource_kind="system",
    ),
    "desktop_fs_stat": DesktopCapabilityDefinition(
        description="读取目标电脑文件或目录的元数据。",
        input_schema=_object({"path": _PATH}, ("path",)),
    ),
    "desktop_fs_list": DesktopCapabilityDefinition(
        description="有界列出目标电脑目录。",
        input_schema=_object({
            "path": _PATH,
            "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
        }, ("path",)),
    ),
    "desktop_fs_read_text": DesktopCapabilityDefinition(
        description="按偏移有界读取目标电脑 UTF-8 文本。",
        input_schema=_object({
            "path": _PATH,
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "max_bytes": {"type": "integer", "minimum": 1, "maximum": 262144, "default": 65536},
        }, ("path",)),
    ),
    "desktop_fs_read_binary": DesktopCapabilityDefinition(
        description="按偏移有界读取目标电脑二进制文件并返回 Base64。",
        input_schema=_object({
            "path": _PATH,
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "max_bytes": {"type": "integer", "minimum": 1, "maximum": 1048576, "default": 262144},
        }, ("path",)),
    ),
    "desktop_fs_write_text": DesktopCapabilityDefinition(
        description="在目标电脑原子写入 UTF-8 文本。",
        input_schema=_object({
            "path": _PATH, "content": {"type": "string"}, "expected_sha256": _HASH,
            "create_parents": {"type": "boolean", "default": True},
        }, ("path", "content")),
        risk_level="medium", writes_external_world=True,
    ),
    "desktop_fs_write_binary": DesktopCapabilityDefinition(
        description="在目标电脑原子写入 Base64 二进制内容。",
        input_schema=_object({
            "path": _PATH, "content_base64": {"type": "string"}, "expected_sha256": _HASH,
            "create_parents": {"type": "boolean", "default": True},
        }, ("path", "content_base64")),
        risk_level="medium", writes_external_world=True,
    ),
    "desktop_fs_edit_text": DesktopCapabilityDefinition(
        description="在目标电脑文本文件中执行精确替换。",
        input_schema=_object({
            "path": _PATH, "old_text": {"type": "string", "minLength": 1},
            "new_text": {"type": "string"}, "replace_all": {"type": "boolean", "default": False},
            "expected_sha256": _HASH,
        }, ("path", "old_text", "new_text")),
        risk_level="medium", writes_external_world=True,
    ),
    "desktop_fs_mkdir": DesktopCapabilityDefinition(
        description="在目标电脑创建目录。",
        input_schema=_object({
            "path": _PATH, "recursive": {"type": "boolean", "default": True},
        }, ("path",)),
        risk_level="medium", writes_external_world=True,
    ),
    "desktop_fs_copy": DesktopCapabilityDefinition(
        description="在目标电脑复制文件或目录。",
        input_schema=_object({
            "source": _PATH, "destination": _PATH,
            "overwrite": {"type": "boolean", "default": False},
        }, ("source", "destination")),
        risk_level="medium", writes_external_world=True,
    ),
    "desktop_fs_move": DesktopCapabilityDefinition(
        description="在目标电脑移动或重命名文件、目录。",
        input_schema=_object({"source": _PATH, "destination": _PATH}, ("source", "destination")),
        risk_level="medium", writes_external_world=True,
    ),
    "desktop_fs_delete": DesktopCapabilityDefinition(
        description="删除目标电脑文件或目录。",
        input_schema=_object({
            "path": _PATH,
            "mode": {"type": "string", "enum": ["trash", "permanent"], "default": "trash"},
        }, ("path",)),
        risk_level="high", requires_confirmation=True, writes_external_world=True,
        can_delete=True, effect_mode="non_repeatable_external",
    ),
    "desktop_exec": DesktopCapabilityDefinition(
        description="在目标电脑指定工作目录运行 Shell 命令。",
        input_schema=_object({
            "command": {"type": "string", "minLength": 1}, "cwd": _PATH,
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600, "default": 120},
            "env": {"type": "object"},
        }, ("command", "cwd")),
        risk_level="high", requires_confirmation=True, writes_external_world=True,
        can_access_secret=True, effect_mode="non_repeatable_external", resource_kind="shell",
        timeout_seconds=660,
    ),
}


def definition_for(capability_id: str) -> DesktopCapabilityDefinition | None:
    return DESKTOP_CAPABILITIES.get(capability_id)


class DesktopActionEnvelope(BaseModel):
    """服务器发送给 Node 的不可变 Action 身份与约束。"""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    action_id: str
    task_id: str
    run_id: str
    idempotency_key: str
    arguments_hash: str
    capability_id: str
    params: dict[str, Any] = Field(default_factory=dict)
    preconditions: dict[str, Any] = Field(default_factory=dict)
    scope: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=30, ge=1, le=3600)
    fencing_token: str


class DesktopActionAck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    action_id: str
    status: Literal["received", "started"]


class DesktopJournalEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action_id: str
    idempotency_key: str
    arguments_hash: str
    status: Literal["received", "started", "committed", "failed", "unknown"]
    result_hash: str = ""
    result: Any = None
    error: str = ""

    @model_validator(mode="after")
    def validate_terminal_receipt(self) -> "DesktopJournalEntry":
        if self.status == "committed" and (
            len(self.result_hash) != 64
            or any(char not in "0123456789abcdefABCDEF" for char in self.result_hash)
        ):
            raise ValueError("committed desktop journal entry requires SHA-256 result_hash")
        return self

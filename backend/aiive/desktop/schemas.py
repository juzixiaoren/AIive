"""Desktop Node WebSocket 与动态工具定义的稳定消息契约。"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


_CAPABILITY_NAME = re.compile(r"^desktop_[a-z0-9_]{1,96}$")


class DesktopCapability(BaseModel):
    """由桌面节点声明、经后端再次校验的单个工具定义。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = Field(min_length=1, max_length=1000)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    risk_level: Literal["low", "medium", "high", "critical"] = "low"
    requires_confirmation: bool = False
    writes_external_world: bool = False
    can_delete: bool = False
    timeout_seconds: float = Field(default=30, ge=1, le=3600)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _CAPABILITY_NAME.fullmatch(normalized):
            raise ValueError("桌面工具名必须使用 desktop_ 前缀和小写字母、数字、下划线")
        return normalized

    @field_validator("input_schema")
    @classmethod
    def validate_input_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        schema = dict(value or {})
        if schema.get("type", "object") != "object":
            raise ValueError("桌面工具 input_schema 顶层必须为 object")
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        schema.setdefault("additionalProperties", False)
        if not isinstance(schema["properties"], dict):
            raise ValueError("桌面工具 input_schema.properties 必须为对象")
        return schema


class DesktopHello(BaseModel):
    """Desktop Node 建立连接后的首个 hello 消息。"""

    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=8, max_length=36)
    name: str = Field(min_length=1, max_length=128)
    platform: Literal["win32", "darwin", "linux"]
    arch: str = Field(min_length=1, max_length=32)
    app_version: str = Field(default="", max_length=32)
    protocol_version: int = Field(default=2, ge=1, le=100)
    auth_token: str = Field(default="", max_length=512)
    capabilities: list[DesktopCapability] = Field(default_factory=list, max_length=64)


class DesktopBindRequest(BaseModel):
    """将一个现有线程绑定到指定桌面节点。"""

    model_config = ConfigDict(extra="forbid")

    thread_id: str = Field(min_length=1, max_length=36)
    node_id: str = Field(min_length=8, max_length=36)


class DesktopOperationResult(BaseModel):
    """Desktop Node 返回的一次工具执行终态。"""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=64)
    action_id: str = Field(default="", max_length=64)
    idempotency_key: str = Field(default="", max_length=160)
    arguments_hash: str = Field(default="", max_length=64)
    status: str = Field(default="", max_length=32)
    result_hash: str = Field(default="", max_length=64)
    idempotent_replay: bool = False
    ok: bool
    result: Any = None
    error: str = Field(default="", max_length=4000)


def capability_dicts(capabilities: list[DesktopCapability]) -> list[dict[str, Any]]:
    """生成适合 JSON 列持久化的能力快照。"""
    return [capability.model_dump(mode="json") for capability in capabilities]

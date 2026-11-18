"""Worker watch parameters shared by function calling and the server."""
from datetime import datetime
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator
from aiive.core.scheduling_time import schedule_instant


def parse_instant(value: str, *, legacy: bool = False) -> datetime:
    return schedule_instant(value, "UTC" if legacy else None)


class WatchModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    next_check_at: str | None = Field(
        default=None, description="Optional earliest poll for file or node. Time watches use config.at.",
    )

    @field_validator("next_check_at")
    @classmethod
    def validate_poll_time(cls, value: str | None) -> str | None:
        return parse_instant(value).isoformat() if value is not None else None


class TimeConfig(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    at: str = Field(description="Trigger instant, e.g. 2026-11-19T09:00:00+08:00.")

    @field_validator("at")
    @classmethod
    def validate_at(cls, value: str) -> str:
        return parse_instant(value).isoformat()


class FileConfig(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", str_strip_whitespace=True)

    path: str = Field(min_length=1, description="Absolute path in an allowed root on the backend host.")
    condition: Literal["exists", "missing", "changed"] = Field(
        default="changed", description="changed compares existence, size and modification time.",
    )
    interval_seconds: int = Field(default=30, ge=5, le=3600)


class NodeConfig(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", str_strip_whitespace=True)

    node_id: str | None = Field(default=None, min_length=1, description="Registered node. Defaults to the task's target.")
    status: Literal["online", "offline"] = "online"
    interval_seconds: int = Field(default=30, ge=5, le=3600)


class TimeWatch(WatchModel):
    watch_type: Literal["time"]
    config: TimeConfig


class FileWatch(WatchModel):
    watch_type: Literal["file"]
    config: FileConfig


class NodeWatch(WatchModel):
    watch_type: Literal["node"]
    config: NodeConfig = Field(default_factory=NodeConfig)


WATCH_PROPOSAL = TypeAdapter(Annotated[TimeWatch | FileWatch | NodeWatch, Field(discriminator="watch_type")])

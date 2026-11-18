"""Domain tool handlers (implementation) split from builtin_tools."""

from aiive.tools.base import BaseTool

from datetime import datetime, timezone
from typing import override, Any
from zoneinfo import ZoneInfo
from aiive.context.run_context import RunContext


class EchoTool(BaseTool):
    @override
    def _run(self, message: str = "") -> str:
        """回显工具：原样返回输入消息。"""
        return message


class GetCurrentTimeTool(BaseTool):
    @override
    def _run(self, ctx: RunContext | None = None) -> dict[str, Any]:
        """Return UTC, server time, and the user time separately."""
        from aiive.core.scheduling_time import user_timezone

        now_utc = datetime.now(timezone.utc)
        now_local = datetime.now(timezone.utc).astimezone()
        offset = now_local.utcoffset()
        offset_seconds = int(offset.total_seconds()) if offset is not None else 0
        if now_local.tzname():
            tz_name = now_local.tzname()
        else:
            sign = "+" if offset_seconds >= 0 else "-"
            abs_off = abs(offset_seconds)
            tz_name = f"UTC{sign}{abs_off // 3600:02d}:{(abs_off % 3600) // 60:02d}"
        user_zone = user_timezone(ctx.user_timezone if ctx else None)
        return {
            "utc": now_utc.isoformat(),
            "local": now_local.isoformat(),
            "timezone_name": tz_name,
            "offset_seconds": offset_seconds,
            "unix_timestamp": int(now_utc.timestamp()),
            "user_timezone": user_zone,
            "user_local": now_utc.astimezone(ZoneInfo(user_zone)).isoformat() if user_zone else None,
        }

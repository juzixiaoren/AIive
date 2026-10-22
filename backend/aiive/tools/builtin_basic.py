"""Domain tool handlers (implementation) split from builtin_tools."""

from datetime import datetime, timezone
from typing import Any


def handle_echo(message: str = "") -> str:
    """回显工具：原样返回输入消息。"""
    return message

def handle_get_current_time() -> dict[str, Any]:
    """获取当前时间（UTC 与服务器本地时区）。

    用途：用户给的是绝对时间点（如“14点提醒我”），但 schedule_reminder 只接受
    delay_minutes（相对分钟数）。Agent 先调用本工具拿到“现在几点”，再自行换算出
    到目标时间还有多少分钟，传给 schedule_reminder，避免乱填 delay。

    返回:
        utc:            当前 UTC 时间（ISO 8601，含 +00:00）
        local:          当前服务器本地时间（ISO 8601，含时区偏移）
        timezone_name:  本地时区名（未知时用 UTC±HH:MM 表示）
        offset_seconds: 本地相对 UTC 的偏移秒数（东八区为 28800）
        unix_timestamp: 当前 Unix 时间戳（秒）
    """
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
    return {
        "utc": now_utc.isoformat(),
        "local": now_local.isoformat(),
        "timezone_name": tz_name,
        "offset_seconds": offset_seconds,
        "unix_timestamp": int(now_utc.timestamp()),
    }

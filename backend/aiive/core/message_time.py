"""Timestamp formatting for model projections of UTC records."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def as_utc(value: datetime) -> datetime:
    # SQLite returns naive values from DateTime(timezone=True).
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def timestamp(value: datetime | None, zone: str = "UTC") -> str:
    return as_utc(value).astimezone(ZoneInfo(zone)).isoformat() if value is not None else "unknown"


def message_stamp(value: datetime | None, source: str, zone: str, *, from_turn: bool = False) -> str:
    basis = " basis=turn" if from_turn else ""
    return f"[time={timestamp(value, zone)} zone={zone} source={source}{basis}]"


def with_message_stamp(content: str, stamp: str) -> str:
    return f"{stamp}\n{content}" if content else stamp

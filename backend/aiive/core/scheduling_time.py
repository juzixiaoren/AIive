"""Absolute scheduling times, with an explicit user IANA time zone for wall times."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def user_timezone(device_timezone: str | None = None) -> str | None:
    from aiive.config import settings

    name = settings.aiive_user_timezone or device_timezone
    if not name:
        return None
    try:
        return ZoneInfo(name).key
    except (ValueError, ZoneInfoNotFoundError):
        return None


def schedule_instant(value: str, zone_name: str | None = None) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("invalid_schedule_time") from error
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc)
    if not zone_name:
        raise ValueError("schedule_time_requires_timezone_or_offset")
    try:
        zone = ZoneInfo(zone_name)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise ValueError("invalid_schedule_timezone") from error
    choices = {
        moment for fold in (0, 1)
        if (moment := parsed.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc))
        .astimezone(zone).replace(tzinfo=None) == parsed
    }
    if len(choices) != 1:
        raise ValueError("ambiguous_or_nonexistent_local_time_requires_offset")
    return choices.pop()

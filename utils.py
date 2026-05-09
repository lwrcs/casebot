from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def tz(timezone_name: str) -> ZoneInfo:
    return ZoneInfo(timezone_name)


def now_in(timezone_name: str) -> datetime:
    return datetime.now(tz(timezone_name))


def to_tz(dt: datetime, timezone_name: str) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz(timezone_name))
    return dt.astimezone(tz(timezone_name))


def offset_str_for(timezone_name: str) -> str:
    """Return the current UTC offset for a timezone as ±HH:MM string."""
    now = now_in(timezone_name)
    offset = now.utcoffset()
    total_seconds = int(offset.total_seconds())
    sign = "+" if total_seconds >= 0 else "-"
    hours, remainder = divmod(abs(total_seconds), 3600)
    minutes = remainder // 60
    return f"{sign}{hours:02d}:{minutes:02d}"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# Convenience alias used by calendar_service._ensure_tz and scheduler
def local_offset_str_for(timezone_name: str) -> str:
    return offset_str_for(timezone_name)

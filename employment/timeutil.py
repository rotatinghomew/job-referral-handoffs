"""Time helpers for the 2026-12-07 UTC+08:00 exchange drill."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Optional

DRILL_DATE = "2026-12-07"
CST = timezone(timedelta(hours=8))
_BARE_TIME = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


def parse_time(value, field: str = "time") -> Optional[datetime]:
    """Parse HH:MM drill times or ISO-8601 values.

    Bare HH:MM values are attached to the published drill date and UTC+08:00.
    Naive ISO values are interpreted in UTC+08:00 because all exchange times
    use that offset.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        # Minute-of-day is convenient for pure domain tests.
        return datetime(2026, 12, 7, tzinfo=CST) + timedelta(minutes=float(value))
    else:
        text = str(value).strip()
        match = _BARE_TIME.fullmatch(text)
        if match:
            hour, minute, second = match.groups()
            return datetime.fromisoformat(
                f"{DRILL_DATE}T{int(hour):02d}:{int(minute):02d}:{int(second or 0):02d}+08:00"
            )
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} must be HH:MM or ISO-8601") from exc

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt.astimezone(CST)


def time_label(value) -> Optional[str]:
    """Return the compact HH:MM label used by the exchange snapshot."""
    dt = parse_time(value)
    if dt is None:
        return None
    return dt.strftime("%H:%M")


def now() -> datetime:
    return datetime.now(CST)


def now_label() -> str:
    return now().isoformat(timespec="seconds")


def is_at_or_before(event_time, as_of) -> bool:
    event = parse_time(event_time)
    cutoff = parse_time(as_of)
    if event is None:
        return True
    if cutoff is None:
        return True
    return event <= cutoff


def strictly_before(value, cutoff) -> bool:
    first = parse_time(value)
    second = parse_time(cutoff)
    if first is None or second is None:
        return False
    return first < second

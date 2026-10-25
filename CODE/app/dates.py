"""Relative date resolution in Europe/Berlin (spec: dates resolved in Europe/Berlin)."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import settings

TZ = ZoneInfo(settings.timezone)


def now_berlin() -> datetime:
    return datetime.now(TZ)


def resolve_relative(base: datetime | None = None,
                     reference: str = "next tuesday") -> datetime:
    """Resolve a simple English relative date. Deterministic, no LLM.

    Supports: today, tomorrow, next <weekday>, <weekday> (this coming one).
    """
    base = base or now_berlin()
    ref = reference.strip().lower()
    weekdays = ["monday", "tuesday", "wednesday", "thursday",
                "friday", "saturday", "sunday"]

    if ref == "today":
        return base
    if ref == "tomorrow":
        return base + timedelta(days=1)

    words = ref.split()
    if words and words[-1] in weekdays:
        target = weekdays.index(words[-1])
        if ref.startswith("next"):
            # "next tuesday": upcoming one; today does not count (=> +7 if today).
            delta = ((target - base.weekday()) - 1) % 7 + 1
        else:
            # bare "tuesday": this coming one; today counts as today.
            delta = (target - base.weekday()) % 7
        return base + timedelta(days=delta)

    raise ValueError(f"unrecognised relative date: {reference!r}")


def in_quiet_hours(dt: datetime) -> bool:
    """True inside the night window (default 02:00-05:00 Berlin)."""
    return settings.reminder_quiet_start <= dt.hour < settings.reminder_quiet_end

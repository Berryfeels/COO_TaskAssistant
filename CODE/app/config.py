"""Settings from environment with spec defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    timezone: str = "Europe/Berlin"
    database_path: str = "coo.db"

    # Pending lifecycle (spec 5.1)
    pending_ttl_hours: int = 24
    reminder_interval_hours: int = 7
    max_reminders: int = 3
    # No reminder is ever sent between 02:00 and 05:00 Berlin (spec 5.1.3)
    reminder_quiet_start: int = 2
    reminder_quiet_end: int = 5

    trello_key: str = ""
    trello_token: str = ""
    trello_board_id: str = ""
    mistral_api_key: str = ""
    mistral_model: str = "mistral-small-latest"


def _load() -> Settings:
    return Settings(
        timezone=_env("TIMEZONE", "Europe/Berlin"),
        database_path=_env("DATABASE_PATH", "coo.db"),
        pending_ttl_hours=int(_env("PENDING_TTL_HOURS", "24")),
        reminder_interval_hours=int(_env("PENDING_REMINDER_INTERVAL_HOURS", "7")),
        max_reminders=int(_env("PENDING_MAX_REMINDERS", "3")),
        reminder_quiet_start=int(_env("REMINDER_QUIET_START", "2")),
        reminder_quiet_end=int(_env("REMINDER_QUIET_END", "5")),
        trello_key=_env("TRELLO_KEY", ""),
        trello_token=_env("TRELLO_TOKEN", ""),
        trello_board_id=_env("TRELLO_BOARD_ID", ""),
        mistral_api_key=_env("MISTRAL_API_KEY", ""),
        mistral_model=_env("MISTRAL_MODEL", "mistral-small-latest"),
    )


settings = _load()

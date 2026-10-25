"""Pending lifecycle service (spec 5.1-5.3).

- 24 h confirmation window
- 3 reminders every 7 hours, never sent 02:00-05:00 Berlin
- cancellation notice after expiry
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from ..config import Settings
from ..dates import in_quiet_hours, now_berlin
from ..schema import Task
from ..storage.db import DB, PendingRow


@dataclass
class PendingDecision:
    """Outcome of matching a user reply against active pending actions."""
    kind: str                     # "applied" | "ambiguous" | "not_confirmation"
    pending_id: Optional[str] = None
    task: Optional[Task] = None
    active: Optional[list[PendingRow]] = None


class PendingService:
    def __init__(self, db: DB, settings: Settings):
        self.db = db
        self.settings = settings

    # -- creation ------------------------------------------------------------
    def save(self, user_id: str, task: Task, created_at: float) -> str:
        return self.db.pending_save(user_id, task, created_at)

    def active(self, user_id: str) -> list[PendingRow]:
        return self.db.pending_active(user_id)

    # -- confirmation matching (spec 5.3) --------------------------------------
    def match_confirmation(self, user_id: str, text: str) -> PendingDecision:
        """A reply is a confirmation if it contains a pending_id of an active
        action, or is a bare affirmative while exactly one action is active."""
        active = self.db.pending_active(user_id)
        if not active:
            return PendingDecision("not_confirmation")

        ids = {p.pending_id for p in active}
        m = re.search(r"\bOK\b|\bYES\b|\bCONFIRM\b", text.upper())
        ref = re.search(r"\b(" + "|".join(ids) + r")\b", text.upper())

        if ref:
            if m:
                return self._apply(ref.group(1))
            return PendingDecision("not_confirmation", active=active)

        if m and len(active) == 1:
            return self._apply(active[0].pending_id)

        if m:
            return PendingDecision("ambiguous", active=active)

        return PendingDecision("not_confirmation", active=active)

    def _apply(self, pending_id: str) -> PendingDecision:
        task = self.db.pending_set_status(pending_id, "applied")
        return PendingDecision("applied", pending_id=pending_id, task=task)

    def apply_by_id(self, pending_id: str) -> Optional[Task]:
        return self.db.pending_set_status(pending_id, "applied")

    # -- reminders (spec 5.1.3) ------------------------------------------------
    def due_for_reminder(self, now: float) -> list[PendingRow]:
        return self.db.pending_due_reminders(
            now, self.settings.reminder_interval_hours, self.settings.max_reminders)

    def quiet_hours(self, dt=None) -> bool:
        return in_quiet_hours(dt or now_berlin())

    def bump_reminder(self, pending_id: str, at: float) -> None:
        self.db.pending_bump_reminder(pending_id, at)

    # -- expiry (spec 5.1.4) ---------------------------------------------------
    def expire(self, now: float) -> list[tuple[str, str, Task]]:
        """Cancel expired pending actions; returns (pending_id, user_id, task)."""
        out = []
        for row in self.db.pending_expired(now, self.settings.pending_ttl_hours):
            task = self.db.pending_set_status(row.pending_id, "cancelled")
            if task:
                out.append((row.pending_id, row.user_id, task))
        return out

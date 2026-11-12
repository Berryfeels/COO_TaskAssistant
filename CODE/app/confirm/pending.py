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
from ..schema import Card
from ..storage.db import DB, PendingRow


@dataclass
class PendingDecision:
    """Outcome of matching a user reply against active pending actions."""
    kind: str                     # "confirm" | "ambiguous" | "not_confirmation"
    pending_id: Optional[str] = None
    card: Optional[Card] = None
    active: Optional[list[PendingRow]] = None


class PendingService:
    def __init__(self, db: DB, settings: Settings):
        self.db = db
        self.settings = settings

    # -- creation ------------------------------------------------------------
    def save(self, user_id: str, card: Card, created_at: float) -> str:
        return self.db.pending_save(user_id, card, created_at)

    def active(self, user_id: str) -> list[PendingRow]:
        return self.db.pending_active(user_id)

    # -- confirmation matching (spec 5.3) --------------------------------------
    def match_confirmation(self, user_id: str, text: str) -> PendingDecision:
        """A reply is a confirmation if it contains a pending_id of an active
        action, or is a bare affirmative while exactly one action is active.

        Read-only: it NEVER changes the pending status. Trello is the source
        of truth, so the DB may be marked "applied" only AFTER the Trello
        write has succeeded (the pipeline calls apply_by_id for that)."""
        active = self.db.pending_active(user_id)
        if not active:
            return PendingDecision("not_confirmation")

        ids = {p.pending_id for p in active}
        m = re.search(r"\bOK\b|\bYES\b|\bCONFIRM\b", text.upper())
        ref = re.search(r"\b(" + "|".join(ids) + r")\b", text.upper())

        if ref:
            if m:
                return self._lookup(ref.group(1))
            return PendingDecision("not_confirmation", active=active)

        if m and len(active) == 1:
            return self._lookup(active[0].pending_id)

        if m:
            return PendingDecision("ambiguous", active=active)

        return PendingDecision("not_confirmation", active=active)

    def _lookup(self, pending_id: str) -> PendingDecision:
        row = self.db.pending_get(pending_id)      # read only, no status change
        if row is None:
            return PendingDecision("not_confirmation")
        return PendingDecision("confirm", pending_id=pending_id, card=row.card)

    def apply_by_id(self, pending_id: str) -> Optional[Card]:
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
    def expire(self, now: float) -> list[tuple[str, str, Card]]:
        """Cancel expired pending actions; returns (pending_id, user_id, card)."""
        out = []
        for row in self.db.pending_expired(now, self.settings.pending_ttl_hours):
            card = self.db.pending_set_status(row.pending_id, "cancelled")
            if card:
                out.append((row.pending_id, row.user_id, card))
        return out

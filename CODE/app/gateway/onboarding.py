"""Onboarding: map a new SimpleX contact to a Trello member (ask their name).

Flow (blueprint V5 section 8, user mapping):
1. First message from an unknown contact -> the bot asks their name on the
   board and consumes the message.
2. The reply is matched against the Trello board members by name
   (case-insensitive token match, same policy as the orchestrator prompt:
   "Stefano" matches "Stefano Di Puma"). Exactly one match -> mapping saved
   to the users table, welcome sent, next message goes to the pipeline.
3. No match or ambiguous match -> the bot re-asks / asks to disambiguate.

The mapping is persistent (users table): a contact is onboarded once.
"""
from __future__ import annotations

from typing import Optional

from ..storage.db import DB
from ..trello.client import TrelloLike


class Onboarding:
    """Gate in front of the pipeline (SimpleXGateway.admit)."""

    def __init__(self, db: DB, trello: TrelloLike, gateway):
        self.db, self.trello, self.gateway = db, trello, gateway
        self._awaiting_name: set[str] = set()

    async def admit(self, user_id: str, message_id: str, text: str) -> bool:
        """True -> the message goes to the pipeline; False -> consumed here."""
        if self.member_id(user_id) is not None:
            return True
        if user_id not in self._awaiting_name:
            self._awaiting_name.add(user_id)
            await self.gateway.send(
                user_id, "Welcome to the COO assistant. "
                          "What is your name on the board?")
            return False
        name = text.strip()
        matches = self._match_member(name)
        if len(matches) == 1:
            member_id, full_name = matches[0]
            self.db.upsert_user(user_id, member_id)
            self._awaiting_name.discard(user_id)
            await self.gateway.send(
                user_id, f"Thanks, {full_name}. You can give me tasks now.")
            return False                         # the name is not a task
        if not matches:
            await self.gateway.send(
                user_id, f"I can't find {name!r} on the board. "
                          "What is your name there?")
        else:
            names = ", ".join(f"{n!r}" for _, n in matches)
            await self.gateway.send(
                user_id, f"Several members match {name!r}: {names}. "
                          "Which one are you (full name)?")
        return False

    def member_id(self, user_id: str) -> Optional[str]:
        row = self.db.user(user_id)
        return row["trello_member_id"] if row else None

    def _match_member(self, name: str) -> list[tuple[str, str]]:
        """Members matching the given name; first-name token match first."""
        wanted = name.casefold().strip()
        if not wanted:
            return []
        members = [(m["id"], m["fullName"]) for m in self.trello.list_members()]
        by_first = [(mid, fn) for mid, fn in members
                    if fn.casefold().split()[0] == wanted]
        if by_first:
            return by_first
        by_full = [(mid, fn) for mid, fn in members
                   if fn.casefold() == wanted]
        return by_full

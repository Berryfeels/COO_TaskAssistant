"""Onboarding: map a new SimpleX contact to a Trello member (ask their name).

Flow (blueprint V5 section 8, user mapping):
1. First message from an unknown contact -> the bot asks their name on the
   board and consumes the message.
2. The reply is matched against the Trello board members by name. A match is
   a CANDIDATE, never a silent mapping: if the user's words are not an exact
   full-name match (e.g. "Stefano" vs "Stefano Di Puma", or a typo like
   "Stephan", or several near-identical members), the bot double-checks first
   - "Are you Stefano Di Puma?" for one candidate, or a list of candidates
   when several match ("Several members match ... Which one are you?").
   Only an affirmative reply (or a reply naming one candidate) saves the
   mapping to the users table. An exact full-name match with exactly one
   member maps immediately.
3. No match -> the bot re-asks. Board unreachable -> a clear error reply, and
   the next message retries (nothing is mapped on failure).

The mapping is persistent (users table): a contact is onboarded once. The
awaiting-name / awaiting-confirmation states are in-memory only: after a
restart the contact simply answers the name question again.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from ..similarity import is_similar, normalize
from ..storage.db import DB
from ..trello.client import TrelloLike

log = logging.getLogger(__name__)

# replies that confirm a candidate ("yes", "sì", "that's me", ...) or reject
# it, matched after normalize() (casefold, punctuation stripped)
AFFIRMATIVE = {"yes", "y", "yeah", "yep", "si", "sì", "ok", "okay", "sure",
               "correct", "that's me", "thats me", "it's me", "its me",
               "i am", "yes i am", "sono", "sono io"}
NEGATIVE = {"no", "nope", "not me", "non sono io", "wrong"}

# first-name tokens are short, so "stephan" vs "stefano" (ratio 0.71) must
# still count as a near-match; full names use the stricter 0.75
NAME_TOKEN_THRESHOLD = 0.7


@dataclass
class Candidate:
    member_id: str
    full_name: str
    exact: bool          # True only for an exact full-name match


class Onboarding:
    """Gate in front of the pipeline (SimpleXGateway.admit)."""

    def __init__(self, db: DB, trello: TrelloLike, gateway):
        self.db, self.trello, self.gateway = db, trello, gateway
        self._awaiting_name: set[str] = set()
        # user_id -> candidates of the pending "are you X?" question
        self._awaiting_confirm: dict[str, list[Candidate]] = {}

    async def admit(self, user_id: str, message_id: str, text: str) -> bool:
        """True -> the message goes to the pipeline; False -> consumed here."""
        if self.member_id(user_id) is not None:
            return True
        if user_id in self._awaiting_confirm:
            return await self._handle_confirmation(user_id, text)
        if user_id not in self._awaiting_name:
            self._awaiting_name.add(user_id)
            await self.gateway.send(
                user_id, "Welcome to the COO assistant. "
                          "What is your name on the board?")
            return False
        return await self._handle_name(user_id, text)

    def member_id(self, user_id: str) -> Optional[str]:
        row = self.db.user(user_id)
        return row["trello_member_id"] if row else None

    # -- name handling ---------------------------------------------------------
    async def _handle_name(self, user_id: str, text: str) -> bool:
        try:
            candidates = self._candidates(text)
        except Exception as e:
            # Trello unreachable or misconfigured: tell the user instead of
            # letting the gateway log a bare pipeline error. The contact
            # stays in _awaiting_name, so the next message retries the match.
            log.error("onboarding: board member lookup failed: %r", e)
            await self.gateway.send(
                user_id, "Sorry, I can't reach the Trello board right now. "
                         "Please try again in a moment.")
            return False
        name = text.strip()
        if not candidates:
            await self.gateway.send(
                user_id, f"I can't find {name!r} on the board. "
                          "What is your name there?")
            return False
        exact = [c for c in candidates if c.exact]
        if len(exact) == 1:
            await self._map(user_id, exact[0])  # unambiguous full name: no
            return False                        # need to double-check
        # one or more NON-exact candidates: there is a chance of doubles, so
        # never map silently - ask the user to confirm which member they are
        self._awaiting_name.discard(user_id)
        self._awaiting_confirm[user_id] = candidates
        if len(candidates) == 1:
            await self.gateway.send(
                user_id, f"Are you {candidates[0].full_name}?")
        else:
            names = ", ".join(f"{c.full_name!r}" for c in candidates)
            await self.gateway.send(
                user_id, f"Several members match {name!r}: {names}. "
                          "Which one are you (full name)?")
        return False

    async def _handle_confirmation(self, user_id: str, text: str) -> bool:
        candidates = self._awaiting_confirm.get(user_id) or []
        reply = normalize(text)
        # the reply names one (or more) of the pending candidates
        named = self._candidates(text, pool=candidates)
        if len(named) == 1:
            await self._map(user_id, named[0])
            return False
        if len(named) > 1:
            names = ", ".join(f"{c.full_name!r}" for c in named)
            await self.gateway.send(
                user_id, f"Several of those match: {names}. "
                          "Which one are you (full name)?")
            return False
        if reply in AFFIRMATIVE:
            if len(candidates) == 1:
                await self._map(user_id, candidates[0])
                return False
            names = ", ".join(f"{c.full_name!r}" for c in candidates)
            await self.gateway.send(
                user_id, f"Which one are you: {names}?")
            return False
        if reply in NEGATIVE:
            self._awaiting_confirm.pop(user_id, None)
            self._awaiting_name.add(user_id)
            await self.gateway.send(
                user_id, "OK - what is your name on the board?")
            return False
        # anything else is treated as a fresh name answer
        self._awaiting_confirm.pop(user_id, None)
        self._awaiting_name.add(user_id)
        return await self._handle_name(user_id, text)

    async def _map(self, user_id: str, candidate: Candidate) -> None:
        self.db.upsert_user(user_id, candidate.member_id)
        self._awaiting_confirm.pop(user_id, None)
        self._awaiting_name.discard(user_id)
        await self.gateway.send(
            user_id, f"Thanks, {candidate.full_name}. You can give me cards now.")

    # -- matching ----------------------------------------------------------------
    def _candidates(self, text: str,
                    pool: Optional[list[Candidate]] = None) -> list[Candidate]:
        """Board members matching the given words, best tier first.

        Tier 1: exact full-name match (unambiguous). Tier 2: exact first-name
        token ("Stefano" -> "Stefano Di Puma"). Tier 3: fuzzy - a near
        token ("Stephan" vs "Stefano") or words contained in the name
        ("di puma"). Tiers 2+3 are always candidates for confirmation,
        never silent mappings.
        """
        wanted = normalize(text)
        if not wanted:
            return []
        if pool is not None:
            members = [(c.member_id, c.full_name) for c in pool]
        else:
            members = [(m["id"], m["fullName"])
                       for m in self.trello.list_members()]
        exact = [Candidate(mid, fn, True) for mid, fn in members
                 if normalize(fn) == wanted]
        if exact:
            return exact
        first = [Candidate(mid, fn, False) for mid, fn in members
                 if fn.casefold().split()[:1] == [wanted.split()[0]]
                 and len(wanted.split()) == 1]
        seen = {c.member_id for c in first}
        fuzzy = []
        wanted_tokens = set(wanted.split())
        for mid, fn in members:
            if mid in seen:
                continue
            name_tokens = set(normalize(fn).split())
            first_token = normalize(fn).split()[0] if normalize(fn) else ""
            if (wanted_tokens and wanted_tokens <= name_tokens) \
                    or is_similar(first_token, wanted,
                                  threshold=NAME_TOKEN_THRESHOLD) \
                    or is_similar(fn, wanted):
                fuzzy.append(Candidate(mid, fn, False))
        return first + fuzzy

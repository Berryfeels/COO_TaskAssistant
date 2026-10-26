"""Orchestrator: intent + extraction in one reasoning step (spec 2.1).

The LLM sits behind a Protocol. MockLLM is deterministic and used in tests;
the real implementation is MistralLLM (see mistral_llm.py, requires
MISTRAL_API_KEY).
"""
from __future__ import annotations

import re
from typing import Optional, Protocol

from ..schema import Action, Task
from ..trello.client import TrelloLike


class LLMProvider(Protocol):
    def interpret(self, text: str, user: dict,
                  trello: TrelloLike) -> Task: ...


class MockLLM:
    """Rule-based stand-in: deterministic, good enough for pipeline tests."""

    MEMBER_MAP = {"marco": "m1", "anna": "m2"}

    def interpret(self, text: str, user: dict, trello: TrelloLike) -> Task:
        t = text.strip()
        t_low = t.lower()
        msg_id = user.get("source_message_id", "")

        # confirmation / denial are handled by the pipeline, not here
        m = re.match(r"^(?:create|new task)\s+(?:task\s+)?['\"]?(.+?)['\"]?\s*(?:for\s+(\w+))?\s*(?:due\s+(.+?))?$",
                     t_low)
        if m:
            # re-match on the original text (same pattern, IGNORECASE) so the
            # title keeps the user's casing
            mo = re.match(r"^(?:create|new task)\s+(?:task\s+)?['\"]?(.+?)['\"]?\s*(?:for\s+(\w+))?\s*(?:due\s+(.+?))?$",
                          t, re.IGNORECASE)
            title = mo.group(1).strip().strip("'\"")
            who = m.group(2)
            due = m.group(3)
            members = trello.list_members()
            member_id = None
            if who:
                matches = [x for x in members if x["fullName"].lower() == who]
                if not matches:
                    from ..schema import Clarification
                    raise Clarification(f"I don't know a team member called {who!r}. Who is it?")
                member_id = matches[0]["id"]
            from ..dates import resolve_relative
            due_iso = str(resolve_relative(reference=due)) if due else None
            return Task(action=Action.create, title=title,
                        assignees=[member_id] if member_id else [],
                        due=due_iso, source_message_id=msg_id)

        m = re.match(r"^(?:move|reschedule)\s+(?:the\s+)?(.+?)\s+(?:to|to)\s+(.+)$", t_low)
        if m:
            card_query, date_ref = m.groups()
            cards = trello.search_cards(card_query)
            if not cards:
                from ..schema import Clarification
                raise Clarification(f"I can't find a task matching {card_query!r}.")
            if len(cards) > 1:
                from ..schema import Clarification
                names = ", ".join(c["name"] for c in cards)
                raise Clarification(f"Several tasks match: {names}. Which one?")
            from ..dates import resolve_relative
            return Task(action=Action.move_date, target_card_id=cards[0]["id"],
                        due=str(resolve_relative(reference=date_ref)),
                        source_message_id=msg_id)

        m = re.match(r"^(?:complete|done|mark done)\s+(?:the\s+)?(.+)$", t_low)
        if m:
            cards = trello.search_cards(m.group(1))
            if len(cards) == 1:
                return Task(action=Action.complete, target_card_id=cards[0]["id"],
                            source_message_id=msg_id)
            from ..schema import Clarification
            raise Clarification(f"I can't uniquely find a task matching {m.group(1)!r}.")

        if t_low.startswith(("what are my tasks", "list my tasks", "show my tasks")):
            return Task(action=Action.query, source_message_id=msg_id)

        return Task(action=Action.query, confidence=0.3,
                    source_message_id=msg_id)

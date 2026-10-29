"""Task model mirroring a Trello card - spec section 4."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Action(str, Enum):
    create = "create"
    update = "update"
    complete = "complete"
    move_date = "move_date"
    archive = "archive"
    query = "query"
    chat = "chat"


WRITE_ACTIONS = {Action.create, Action.update, Action.complete,
                 Action.move_date, Action.archive}


class Clarification(Exception):
    """Orchestrator needs more info from the user before proceeding."""

    def __init__(self, question: str):
        self.question = question
        super().__init__(question)


@dataclass
class Task:
    """Structured output of the Orchestrator (spec section 4)."""
    action: Action
    target_card_id: Optional[str] = None      # null for create
    title: str = ""
    description: str = ""
    assignees: list[str] = field(default_factory=list)   # Trello member IDs
    due: Optional[str] = None                 # ISO 8601
    done: bool = False                        # dueComplete
    closed: bool = False                      # archived card (Trello 'closed')
    list_id: Optional[str] = None             # idList
    labels: list[str] = field(default_factory=list)      # idLabels
    confidence: float = 1.0
    reply: str = ""                        # free-text answer (action chat)
    source_message_id: str = ""

    MIN_CONFIDENCE = 0.7

    @property
    def needs_clarification(self) -> bool:
        return self.confidence < self.MIN_CONFIDENCE

    @property
    def is_write(self) -> bool:
        return self.action in WRITE_ACTIONS

    @property
    def has_change(self) -> bool:
        """Whether this task carries at least one field to write."""
        return bool(self.title or self.description or self.due is not None
                    or self.list_id or self.assignees or self.done
                    or self.closed)

    def validate(self) -> None:
        if self.is_write and self.action != Action.create and not self.target_card_id:
            raise ValueError(f"{self.action.value} requires target_card_id")
        if self.action == Action.create and not self.title:
            raise ValueError("create requires a title")
        if self.action == Action.move_date and self.due is None:
            raise ValueError("move_date requires the new due date")
        if self.action == Action.update and not self.has_change:
            raise ValueError("update requires a field to change (title, "
                             "description, due date, list, assignees, "
                             "done or closed)")

    @classmethod
    def from_card(cls, card: dict) -> "Task":
        """Reverse mapping: a Trello card (e.g. from a webhook event) -> Task.
        Used to verify the mirror and to build notifications."""
        return cls(
            action=Action.query,
            target_card_id=card.get("id"),
            title=card.get("name", ""),
            description=card.get("desc", ""),
            assignees=list(card.get("idMembers") or []),
            due=card.get("due"),
            done=bool(card.get("dueComplete")),
            closed=bool(card.get("closed")),
            list_id=card.get("idList"),
            labels=list(card.get("idLabels") or []),
        )

    def mirror_diff(self, card: dict) -> dict:
        """Fields where this Task and a Trello card disagree (mirror audit)."""
        other = Task.from_card(card)
        diffs = {}
        for f in ("title", "description", "assignees", "due", "done",
                  "closed", "list_id", "labels"):
            a, b = getattr(self, f), getattr(other, f)
            if a is not None and a != b:
                diffs[f] = {"task": a, "card": b}
        return diffs

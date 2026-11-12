"""Local mirror of the Trello board structure (DOCS/Trello_schema.md).

Board 1--< TrelloList (a column) 1--< Card (a card) - spec section 4.
`Card` is the structured output of the Orchestrator; its card fields map
to Trello card field names via Card.CARD_FIELD_MAP.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Action(str, Enum):
    add_card = "add_card"
    update = "update"
    complete = "complete"
    move_date = "move_date"
    archive = "archive"
    add_list = "add_list"
    query = "query"
    chat = "chat"


WRITE_ACTIONS = {Action.add_card, Action.update, Action.complete,
                 Action.move_date, Action.archive, Action.add_list}

# Action names used before the add_card rename: still present in stored
# pending rows and occasionally still emitted by the model.
_LEGACY_ACTION_NAMES = {"create": Action.add_card}


def parse_action(value) -> Action:
    """Parse an action name; legacy names map to their renamed action.
    Raises ValueError on unknown names."""
    if isinstance(value, Action):
        return value
    if value in _LEGACY_ACTION_NAMES:
        return _LEGACY_ACTION_NAMES[value]
    return Action(value)


class Clarification(Exception):
    """Orchestrator needs more info from the user before proceeding."""

    def __init__(self, question: str):
        self.question = question
        super().__init__(question)


@dataclass
class Board:
    """Mirror of the Trello board object (Trello_schema.md section 2)."""
    id: str = ""
    name: str = ""
    desc: str = ""
    closed: bool = False
    url: str = ""
    short_url: str = ""

    @classmethod
    def from_api(cls, board: dict) -> "Board":
        return cls(
            id=board.get("id", ""),
            name=board.get("name", ""),
            desc=board.get("desc", ""),
            closed=bool(board.get("closed")),
            url=board.get("url", ""),
            short_url=board.get("shortUrl", ""),
        )


@dataclass
class TrelloList:
    """Mirror of a Trello list - what the Trello UI calls a column
    (Trello_schema.md section 3). Its `id` is the cards' `idList`.
    Columns created via add_list are mirrored in the DB `lists` table."""
    id: str = ""
    name: str = ""
    closed: bool = False
    id_board: str = ""
    pos: float = 0.0
    subscribed: bool = False

    @classmethod
    def from_api(cls, lst: dict) -> "TrelloList":
        return cls(
            id=lst.get("id", ""),
            name=lst.get("name", ""),
            closed=bool(lst.get("closed")),
            id_board=lst.get("idBoard", ""),
            pos=lst.get("pos", 0.0),
            subscribed=bool(lst.get("subscribed")),
        )


@dataclass
class Card:
    """Structured output of the Orchestrator; mirrors a Trello card
    (spec section 4, Trello_schema.md section 4)."""
    # Card field -> Trello API card field (un-annotated: not a dataclass field)
    CARD_FIELD_MAP = {
        "target_card_id": "id",
        "title": "name",
        "description": "desc",
        "assignees": "idMembers",
        "due": "due",
        "done": "dueComplete",
        "closed": "closed",
        "list_id": "idList",
        "labels": "idLabels",
    }

    action: Action
    target_card_id: Optional[str] = None      # null for add_card
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
        """Whether this card carries at least one field to write."""
        return bool(self.title or self.description or self.due is not None
                    or self.list_id or self.assignees or self.done
                    or self.closed)

    def validate(self) -> None:
        if self.is_write and self.action not in (Action.add_card, Action.add_list) \
                and not self.target_card_id:
            raise ValueError(f"{self.action.value} requires target_card_id")
        if self.action == Action.add_card and not self.title:
            raise ValueError("add_card requires a title")
        if self.action == Action.add_list and not self.title:
            # a column is always created; an unnamed one gets the default
            # (the LLM may return title null for add_list, so never block)
            self.title = "new List"
        if self.action == Action.move_date and self.due is None:
            raise ValueError("move_date requires the new due date")
        if self.action == Action.update and not self.has_change:
            raise ValueError("update requires a field to change (title, "
                             "description, due date, list, assignees, "
                             "done or closed)")

    @classmethod
    def from_api(cls, card: dict) -> "Card":
        """Reverse mapping: a Trello card dict (GET /1/cards/{id}, webhook
        event) -> Card, via CARD_FIELD_MAP. Used to verify the mirror and to
        build notifications."""
        values = {}
        for local_field, trello_field in cls.CARD_FIELD_MAP.items():
            v = card.get(trello_field)
            if local_field in ("assignees", "labels"):
                v = list(v or [])
            elif local_field in ("done", "closed"):
                v = bool(v)
            elif local_field in ("title", "description"):
                v = v or ""
            values[local_field] = v
        return cls(action=Action.query, **values)

    def to_api(self) -> dict:
        """Partial Trello card object in Trello's field names: only the
        fields this card actually carries (Trello_schema.md section 4)."""
        card = {}
        for local_field, trello_field in self.CARD_FIELD_MAP.items():
            v = getattr(self, local_field)
            if v is None or v == "" or v == [] or v is False:
                continue
            card[trello_field] = v
        return card

    def mirror_diff(self, card: dict) -> dict:
        """Fields where this Card and a Trello card disagree (mirror audit)."""
        other = Card.from_api(card)
        diffs = {}
        for f in ("title", "description", "assignees", "due", "done",
                  "closed", "list_id", "labels"):
            a, b = getattr(self, f), getattr(other, f)
            if a is not None and a != b:
                diffs[f] = {"card": a, "trello": b}
        return diffs

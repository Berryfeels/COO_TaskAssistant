"""Shared harness for pipeline-level (integration) and system tests.

FakeTrello: in-memory Trello board implementing everything the pipeline and
the onboarding gate use (apply/create/update/lists/members), with scriptable
failures. FakeLLM: scripted LLMProvider. make_pipeline() wires a REAL
Pipeline with an in-memory DB and a capturing Gateway, so integration tests
exercise the production code path (spec 9.1) end to end minus the network.
"""
from __future__ import annotations

import asyncio
import itertools
from types import SimpleNamespace

from app.config import Settings
from app.confirm.pending import PendingService
from app.pipeline import Gateway, Pipeline
from app.schema import Action, Card
from app.storage.db import DB


def run(coro):
    return asyncio.run(coro)


def incoming(pipe, user_id, message_id, text):
    """One real message through the production entry point."""
    return run(pipe.handle_incoming(user_id, message_id, text))


def replies_to(gateway, user_id):
    return [t for u, t in gateway.sent if u == user_id]


def last_reply(gateway, user_id=None):
    msgs = gateway.sent if user_id is None else [
        (u, t) for u, t in gateway.sent if u == user_id]
    return msgs[-1][1]


class FakeTrello:
    """In-memory TrelloLike. `cards`/`lists` are dicts shaped exactly like
    the Trello API responses the code consumes."""

    def __init__(self, default_list="Ltodo"):
        self.default_list = default_list
        self.cards: list[dict] = []
        self.lists = [{"id": "Ltodo", "name": "To-do"},
                      {"id": "Ldoing", "name": "Doing"},
                      {"id": "Ldone", "name": "Done"}]
        self.members = [{"id": "m1", "fullName": "Stefano Di Puma"},
                        {"id": "m2", "fullName": "Marco Rossi"}]
        self.calls: list[tuple] = []
        self.fail_with: Exception | None = None   # persistent failure
        self._fail_next: Exception | None = None  # one-shot failure
        self._ids = itertools.count(1)

    # -- failure scripting ----------------------------------------------------
    def fail_next(self, exc: Exception | None = None) -> None:
        self._fail_next = exc or RuntimeError("HTTP 500")

    def _maybe_fail(self) -> None:
        if self._fail_next is not None:
            exc, self._fail_next = self._fail_next, None
            raise exc
        if self.fail_with is not None:
            raise self.fail_with

    # -- writes ---------------------------------------------------------------
    def apply(self, card: Card) -> dict:
        self.calls.append(("apply", card))
        if card.action == Action.add_card:
            return self.create_card(card)
        if card.action == Action.add_list:
            return self.create_list(card.title)
        return self.update_card(card)

    def create_card(self, card: Card) -> dict:
        self.calls.append(("create_card", card))
        self._maybe_fail()
        c = {"id": f"c{next(self._ids)}",
             "name": card.title,
             "desc": card.description or "",
             "due": card.due,
             "dueComplete": bool(card.done),
             "closed": bool(card.closed),
             "idList": card.list_id or self.default_list,
             "idMembers": list(card.assignees),
             "idLabels": list(card.labels)}
        self.cards.append(c)
        return c

    def update_card(self, card: Card) -> dict:
        self.calls.append(("update_card", card))
        self._maybe_fail()
        t = next((c for c in self.cards if c["id"] == card.target_card_id), None)
        if t is None:
            raise RuntimeError(f"HTTP 404 unknown card {card.target_card_id}")
        if card.title:
            t["name"] = card.title
        if card.description:
            t["desc"] = card.description
        if card.due is not None:
            t["due"] = card.due
        if card.action == Action.complete or card.done:
            t["dueComplete"] = True
        if card.action == Action.archive or card.closed:
            t["closed"] = True
        if card.list_id:
            t["idList"] = card.list_id
        if card.assignees:
            t["idMembers"] = list(card.assignees)
        return t

    def create_list(self, name: str) -> dict:
        self.calls.append(("create_list", name))
        self._maybe_fail()
        lst = {"id": f"L{next(self._ids)}", "name": name, "closed": False,
               "idBoard": "board1", "pos": 0.0, "subscribed": False}
        self.lists.append(lst)
        return lst

    # -- reads ----------------------------------------------------------------
    def board_cards(self) -> list[dict]:
        self.calls.append(("board_cards",))
        return list(self.cards)

    def search_cards(self, query: str) -> list[dict]:
        self.calls.append(("search_cards", query))
        q = query.casefold()
        return [c for c in self.cards if q in (c.get("name") or "").casefold()]

    def get_card(self, card_id: str):
        self.calls.append(("get_card", card_id))
        return next((c for c in self.cards if c["id"] == card_id), None)

    def list_members(self) -> list[dict]:
        self.calls.append(("list_members",))
        return list(self.members)

    def list_lists(self) -> list[dict]:
        self.calls.append(("list_lists",))
        return list(self.lists)


class FakeLLM:
    """Scripted LLMProvider: each interpret() pops the next item of the
    script - a Card to return, or an Exception to raise. Records every call
    as (text, user)."""

    def __init__(self, script=()):
        self.script = list(script)
        self.calls: list[tuple[str, dict]] = []

    def interpret(self, text, user, trello) -> Card:
        self.calls.append((text, dict(user)))
        if not self.script:
            raise AssertionError(f"FakeLLM: unexpected interpret({text!r})")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_pipeline(script=(), trello=None, settings=None, gateway=None, db=None):
    """A real Pipeline wired to in-memory fakes; returns all components."""
    db = db or DB(":memory:")
    trello = trello or FakeTrello()
    llm = FakeLLM(script)
    settings = settings or Settings()
    pending = PendingService(db, settings)
    gateway = gateway or Gateway()
    pipe = Pipeline(db, trello, llm, pending, gateway, settings)
    return SimpleNamespace(pipe=pipe, db=db, trello=trello, llm=llm,
                           pending=pending, gateway=gateway, settings=settings)


def add(title, **kw) -> Card:
    kw.setdefault("action", Action.add_card)
    kw["title"] = title
    return Card(**kw)


def chat(reply="ok", **kw) -> Card:
    kw.setdefault("action", Action.chat)
    kw["reply"] = reply
    return Card(**kw)


def sx_frame(body, display="Stefano", item_id=1) -> dict:
    """A server-initiated SimpleX frame ({"resp": {...}}, no corrId) carrying
    one received direct text message - the v7.0.0.12 newChatItems shape."""
    return {"resp": {"type": "newChatItems", "user": {},
                     "chatItems": [{
                         "chatInfo": {"type": "direct",
                                      "contact": {"contactId": 12,
                                                  "localDisplayName": display}},
                         "chatItem": {"chatDir": {"type": "directRcv"},
                                      "meta": {"itemId": item_id},
                                      "content": {"type": "rcvMsgContent",
                                                  "msgContent": {"type": "text",
                                                                 "text": body}}}}]}}

"""Trello REST API wrapper (spec section 6) + in-memory fake for tests.

Real network calls live in TrelloClient; FakeTrello implements the same
protocol so the pipeline and tests run offline.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Optional, Protocol

from ..schema import Action, Task


class TrelloLike(Protocol):
    def create_card(self, task: Task) -> dict: ...
    def update_card(self, task: Task) -> dict: ...
    def search_cards(self, query: str) -> list[dict]: ...
    def get_card(self, card_id: str) -> Optional[dict]: ...
    def list_members(self) -> list[dict]: ...


class TrelloClient:
    """Thin wrapper over the Trello REST API (spec 6.2)."""

    BASE = "https://api.trello.com/1"

    def __init__(self, key: str, token: str, board_id: str):
        self.key, self.token = key, token
        self._board_id = board_id          # may be a short ID (URL slug)
        self._full_board_id: str | None = None

    @property
    def board_id(self) -> str:
        """Full board ID, resolved lazily. Most endpoints accept the short
        ID, but /1/search's idBoards filter requires the full 24-char ID."""
        if self._full_board_id is None and self._board_id:
            self._full_board_id = self._req(
                "GET", f"/boards/{self._board_id}", {"fields": "id"})["id"]
        return self._full_board_id or self._board_id

    def _req(self, method: str, path: str, params: dict) -> object:
        params = {**params, "key": self.key, "token": self.token}
        # drop None/empty values; Trello only accepts query parameters
        params = {k: v for k, v in params.items() if v not in (None, "", [])}
        url = f"{self.BASE}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, method=method)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode() or "{}")

    # -- writes -------------------------------------------------------------
    def create_card(self, task: Task) -> dict:
        return self._req("POST", "/cards", {
            "idList": task.list_id, "name": task.title, "desc": task.description,
            "due": task.due, "idMembers": ",".join(task.assignees),
            "idLabels": ",".join(task.labels)})

    def update_card(self, task: Task) -> dict:
        params: dict = {"name": task.title, "desc": task.description}
        if task.due is not None:
            params["due"] = task.due
        if task.action == Action.complete:
            params["dueComplete"] = "true"
        if task.action == Action.archive:
            params["closed"] = "true"
        if task.list_id:
            params["idList"] = task.list_id
        if task.assignees:
            params["idMembers"] = ",".join(task.assignees)
        return self._req("PUT", f"/cards/{task.target_card_id}", params)

    def apply(self, task: Task) -> dict:
        """Apply a confirmed write action (called only after user confirmation)."""
        return self.create_card(task) if task.action == Action.create \
            else self.update_card(task)

    # -- reads (Orchestrator tools, spec 2.2) --------------------------------
    def board_cards(self) -> list[dict]:
        """All cards on the board. Deterministic read: unlike /1/search,
        this endpoint reflects new cards immediately (no index lag).
        Preferred for card resolution."""
        return self._req("GET", f"/boards/{self.board_id}/cards", {"fields": "id,name,desc,due,dueComplete,closed,idList,idMembers,idLabels"})

    def search_cards(self, query: str) -> list[dict]:
        """Text search. NOTE: Trello's search index lags behind for recently
        created/renamed cards (observed > 40 s). Prefer board_cards()."""
        res = self._req("GET", "/search", {
            "query": query, "idBoards": self.board_id,
            "modelTypes": "cards"})
        return res.get("cards", [])

    def get_card(self, card_id: str) -> Optional[dict]:
        return self._req("GET", f"/cards/{card_id}", {})

    def list_members(self) -> list[dict]:
        return self._req("GET", f"/boards/{self.board_id}/members", {})


class FakeTrello:
    """In-memory implementation for tests and local development."""

    def __init__(self):
        self.cards: dict[str, dict] = {}
        self._next = 0

    def create_card(self, task: Task) -> dict:
        self._next += 1
        card_id = f"card{self._next}"
        self.cards[card_id] = {
            "id": card_id, "name": task.title, "desc": task.description,
            "due": task.due, "idMembers": task.assignees,
            "idLabels": task.labels, "idList": task.list_id,
            "closed": False, "dueComplete": False}
        return self.cards[card_id]

    def update_card(self, task: Task) -> dict:
        card = self.cards[task.target_card_id]
        if task.title:
            card["name"] = task.title
        if task.description:
            card["desc"] = task.description
        if task.due is not None:
            card["due"] = task.due
        if task.action == Action.complete:
            card["dueComplete"] = True
        if task.action == Action.archive:
            card["closed"] = True
        if task.list_id:
            card["idList"] = task.list_id
        if task.assignees:
            card["idMembers"] = task.assignees
        return card

    def apply(self, task: Task) -> dict:
        return self.create_card(task) if task.action == Action.create \
            else self.update_card(task)

    def search_cards(self, query: str) -> list[dict]:
        q = query.lower()
        return [c for c in self.cards.values()
                if q in c["name"].lower() and not c["closed"]]

    def board_cards(self) -> list[dict]:
        return list(self.cards.values())

    def get_card(self, card_id: str) -> Optional[dict]:
        return self.cards.get(card_id)

    def list_members(self) -> list[dict]:
        return [{"id": "m1", "fullName": "Marco"},
                {"id": "m2", "fullName": "Anna"}]

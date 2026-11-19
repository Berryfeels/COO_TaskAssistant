"""Trello REST API wrapper (spec section 6).

Real network calls live in TrelloClient - the bot talks to the real Trello
board only.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional, Protocol

from ..schema import Action, Card

log = logging.getLogger(__name__)


class TrelloLike(Protocol):
    def create_card(self, card: Card) -> dict: ...
    def update_card(self, card: Card) -> dict: ...
    def create_list(self, name: str) -> dict: ...
    def search_cards(self, query: str) -> list[dict]: ...
    def get_card(self, card_id: str) -> Optional[dict]: ...
    def list_members(self) -> list[dict]: ...
    def list_lists(self) -> list[dict]: ...


class TrelloClient:
    """Thin wrapper over the Trello REST API (spec 6.2)."""

    BASE = "https://api.trello.com/1"

    def __init__(self, key: str, token: str, board_id: str,
                 default_list_id: str = ""):
        self.key, self.token = key, token
        self._board_id = board_id          # may be a short ID (URL slug)
        self.default_list_id = default_list_id   # Trello requires idList on create
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
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            # Trello's response body says WHY (e.g. "invalid id" for an empty
            # board id, "invalid key" for bad credentials): log it so the bot
            # log alone explains the failure. The URL is never logged (it
            # carries key+token).
            body = e.read().decode(errors="replace")[:200]
            log.error("Trello %s %s -> HTTP %s: %s", method, path, e.code, body)
            raise

    # -- writes -------------------------------------------------------------
    def create_card(self, card: Card) -> dict:
        # idList is required by Trello: fall back to the configured default
        # list when the orchestrator left it empty (no column named)
        return self._req("POST", "/cards", {
            "idList": card.list_id or self.default_list_id,
            "name": card.title, "desc": card.description,
            "due": card.due, "idMembers": ",".join(card.assignees),
            "idLabels": ",".join(card.labels)})

    def update_card(self, card: Card) -> dict:
        params: dict = {"name": card.title, "desc": card.description}
        if card.due is not None:
            params["due"] = card.due
        if card.action == Action.complete or card.done:
            params["dueComplete"] = "true"
        if card.action == Action.archive or card.closed:
            params["closed"] = "true"
        if card.list_id:
            params["idList"] = card.list_id
        if card.assignees:
            params["idMembers"] = ",".join(card.assignees)
        return self._req("PUT", f"/cards/{card.target_card_id}", params)

    def create_list(self, name: str) -> dict:
        """Add a column (list) to the board: POST /lists."""
        return self._req("POST", "/lists", {
            "idBoard": self.board_id, "name": name})

    def apply(self, card: Card) -> dict:
        """Apply a confirmed write action (called only after user confirmation)."""
        if card.action == Action.add_card:
            return self.create_card(card)
        if card.action == Action.add_list:
            return self.create_list(card.title)
        return self.update_card(card)

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

    def list_lists(self) -> list[dict]:
        """Board columns (To-do, Doing, Done, ...). The orchestrator resolves
        a column NAME mentioned by the user to its real idList via this."""
        return self._req("GET", f"/boards/{self.board_id}/lists",
                         {"fields": "id,name"})


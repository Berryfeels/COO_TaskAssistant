"""Mistral LLM Orchestrator: intent + extraction with tool use (spec 2.1).

Uses the Mistral API (chat completions with function calling) through the
standard library only - no SDK dependency. The model gets the same Trello
read tools as every other implementation of LLMProvider:

    search_cards(query) | get_card(card_id) | list_members()

and must answer with the Task object as JSON (spec section 4).

Config: MISTRAL_API_KEY, MISTRAL_MODEL (default mistral-small-latest).
"""
from __future__ import annotations

import json
import re
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..config import settings
from ..schema import Action, Clarification, Task
from ..trello.client import TrelloLike

API_URL = "https://api.mistral.ai/v1/chat/completions"

SYSTEM_PROMPT = """You are the task-management assistant of a COO team.
The user speaks English. Every user message contains either a NEW task or a
MODIFICATION of an existing task. Return ONLY a JSON object with these fields:
action (create|update|complete|move_date|archive|query), target_card_id (null
for create), title, description, assignees (Trello member IDs, resolved via
list_members), due (ISO 8601, resolved in Europe/Berlin), done (bool),
list_id, labels, confidence (0-1), source_message_id.
If anything is ambiguous (several cards match, unknown person, incomplete
date), set confidence below 0.7 and add a "question" field with ONE short
clarifying question in English. Never invent card or member IDs: use the
tools to resolve them. Today is {today} ({weekday}, Europe/Berlin).
"""

_TOOLS = [
    {"type": "function", "function": {
        "name": "board_cards",
        "description": "List all cards on the board (id, name, due, list). "
                       "Deterministic: reflects new cards immediately. "
                       "Prefer this for resolving which card the user means.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "search_cards",
        "description": "Search open Trello cards by text.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "get_card",
        "description": "Fetch one Trello card by ID.",
        "parameters": {"type": "object", "properties": {
            "card_id": {"type": "string"}},
            "required": ["card_id"]}}},
    {"type": "function", "function": {
        "name": "list_members",
        "description": "List board members (id, fullName).",
        "parameters": {"type": "object", "properties": {}}}},
]


class MistralLLM:
    def __init__(self, api_key: str | None = None,
                 model: str | None = None):
        self.api_key = api_key or settings.mistral_api_key
        self.model = model or "mistral-small-latest"

    # -- API plumbing ---------------------------------------------------------
    def _complete(self, messages: list[dict]) -> dict:
        body = json.dumps({
            "model": self.model, "messages": messages,
            "tools": _TOOLS, "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }).encode()
        req = urllib.request.Request(API_URL, data=body, method="POST")
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())["choices"][0]["message"]

    # -- tool dispatch ----------------------------------------------------------
    def _call_tool(self, name: str, args: dict, trello: TrelloLike):
        if name == "board_cards":
            return trello.board_cards()
        if name == "search_cards":
            return trello.search_cards(args.get("query", ""))
        if name == "get_card":
            return trello.get_card(args.get("card_id", ""))
        if name == "list_members":
            return trello.list_members()
        raise ValueError(f"unknown tool {name}")

    # -- LLMProvider protocol -----------------------------------------------------
    def interpret(self, text: str, user: dict, trello: TrelloLike) -> Task:
        tz = ZoneInfo(settings.timezone)
        today = datetime.now(tz)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT.format(
                today=today.date().isoformat(),
                weekday=today.strftime("%A"))},
            {"role": "user", "content": text},
        ]

        for _ in range(5):                       # tool-use loop, bounded
            msg = self._complete(messages)
            calls = msg.get("tool_calls") or []
            if not calls:
                return self._parse(msg["content"], user, today)
            messages.append(msg)
            for call in calls:
                fn = call["function"]
                args = json.loads(fn.get("arguments") or "{}")
                messages.append({
                    "role": "tool", "tool_call_id": call["id"],
                    "name": fn["name"],
                    "content": json.dumps(self._call_tool(fn["name"], args,
                                                          trello))})
        raise Clarification("I could not resolve that. Can you rephrase?")

    # -- output parsing -----------------------------------------------------------
    def _parse(self, content: str, user: dict, today: datetime) -> Task:
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            raise Clarification("I did not understand. Can you rephrase?")
        data = json.loads(m.group(0))

        question = data.pop("question", None)
        data["action"] = Action(data.get("action", "query"))
        data.setdefault("source_message_id", user.get("source_message_id", ""))
        task = Task(**{k: v for k, v in data.items()
                       if k in Task.__dataclass_fields__})
        if question:
            task.confidence = min(task.confidence, 0.5)
            raise Clarification(question)
        return task

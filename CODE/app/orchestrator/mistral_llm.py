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
import logging
import re
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..config import settings
from ..schema import Action, Clarification, Task
from ..trello.client import TrelloLike

log = logging.getLogger(__name__)

API_URL = "https://api.mistral.ai/v1/chat/completions"

SYSTEM_PROMPT = """You manage the
team's tasks on Trello. You are the FIRST to read every user message and
you own the conversation, which is free: greetings, small talk and general
questions are normal and are answered naturally. The user may write in any
language, reply in the same language but ALWAYS write the
card content in English.
A user message is free conversation OR a NEW task OR a MODIFICATION of an
existing task. Return ONLY a JSON object with these fields:
action (create|update|complete|move_date|archive|query|chat), reply (your
free-text answer in the user's language; required and only used for action
"chat"; never force a task when the user is only talking), target_card_id
(null for create), title (in English; if the user does not state a title, generate
a short English title from the request), description (in English; collect
there every other useful detail - constraints, contact person with email and
phone, location, notes - and translate EVERY word to English, e.g. Italian
"maestra" becomes "teacher"), assignees (Trello member IDs of the
responsible person: in "X must do Y" the subject X is ALWAYS the responsible
member - ALWAYS call list_members and match X against the members' full
names by first name (e.g. "Stefano" matches "Stefano Di Puma") BEFORE asking
anything; put the member ID in assignees whenever the person is on the
board; only ask a clarifying question if no member matches), due
(ISO 8601 date AND time when a time is given, with
the explicit Europe/Berlin UTC offset, e.g. "2026-11-17T08:00:00+01:00"),
done (bool), list_id (null for create unless the user names a column; the system places
new cards in the default list - never guess or invent a list ID),
labels (always [] for now: label resolution is not implemented, never invent
label IDs), confidence (0-1), source_message_id.
If anything is ambiguous (several cards match, unknown person, incomplete
or inconsistent date), set confidence below 0.7 and add a "question" field
with ONE short clarifying question. Never invent card or member IDs: use the
tools to resolve them. Every modify action (update|move_date|complete|
archive) MUST carry a real target_card_id: ALWAYS call board_cards first and
match the user's task description against the card names to resolve it.
Returning a modify action without calling board_cards is never acceptable -
if no card matches, set confidence below 0.7 and ask which task is meant.
A modification without an extractable change is never
valid: for move_date the NEW date goes in due; for update at least one of
title, description, due, list_id, assignees, done, closed must change. If
you cannot extract the change, set confidence below 0.7 and ask for it
instead of returning the action.
For action update, return ONLY the fields the user wants to change and
leave every other field null/empty - empty means "leave unchanged". NEVER
derive the title from the user's request: if the user does not explicitly
ask to rename, return title null. To add a note or information to a task,
call get_card on the target first and return description = the EXISTING
description with the new note APPENDED to it (in English, as a clear
instruction like "make it black and white") - never a paraphrase of the
user's whole request, never a replacement of what is already there.
Today is {today} ({weekday}, Europe/Berlin).
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
    MAX_HISTORY = 12                     # remembered turns per user

    def __init__(self, api_key: str | None = None,
                 model: str | None = None):
        self.api_key = api_key or settings.mistral_api_key
        self.model = model or "mistral-small-latest"
        self._history: dict[str, list[dict]] = {}   # per-user conversation

    # -- API plumbing ---------------------------------------------------------
    def _complete(self, messages: list[dict]) -> dict:
        # NOTE: response_format json_object must NOT be set here: combined
        # with tools it makes the model loop on tool calls without ever
        # emitting the final answer (observed on mistral-small-latest).
        # _parse() extracts the JSON from the (possibly fenced) content.
        body = json.dumps({
            "model": self.model, "messages": messages,
            "tools": _TOOLS, "temperature": 0.2,
        }).encode()
        req = urllib.request.Request(API_URL, data=body, method="POST")
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=60) as resp:
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
        # free conversation: the user's previous turns are part of the
        # context, so greetings and follow-ups read naturally
        user_id = str(user.get("user_id", ""))
        history = self._history.setdefault(user_id, [])
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT.format(
                today=today.date().isoformat(),
                weekday=today.strftime("%A"))},
            *history[-self.MAX_HISTORY:],
            {"role": "user", "content": text},
        ]

        for _ in range(5):                       # tool-use loop, bounded
            msg = self._complete(messages)
            calls = msg.get("tool_calls") or []
            if not calls:
                # remember the exchange for the next message
                history.append({"role": "user", "content": text})
                history.append({"role": "assistant",
                                "content": msg["content"]})
                del history[:-self.MAX_HISTORY]
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
            # The model sometimes answers free conversation in plain prose
            # (no JSON envelope). The prose IS the chat reply: deliver it
            # instead of rejecting it with a canned error.
            text = content.strip()
            if text:
                log.info("LLM answered in free text; treating as chat")
                return Task(action=Action.chat, reply=text,
                            source_message_id=str(
                                user.get("source_message_id", "")))
            log.warning("LLM sent neither JSON nor text")
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
        log.info("LLM -> %s (confidence %.2f)", task.action.value,
                 task.confidence)
        return task

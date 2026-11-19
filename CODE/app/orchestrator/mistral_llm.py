"""Mistral LLM Orchestrator: the LLM speaks in plain words, a parser fills
the JSON (spec 2.1 steps 3-4).

Uses the Mistral API (chat completions with function calling) through the
standard library only - no SDK dependency. The model gets the Trello read
tools:

    board_cards() | search_cards(query) | get_card(card_id) |
    list_members() | list_lists()

and answers with PLAIN WORDS ONLY - never JSON: either free prose (chat) or
one labelled line per field (action:, title:, card:, assign:, due:, list:,
note:, question:). WordParser (parser.py) is the deterministic program logic
that turns those words into the Card JSON, resolving names to Trello IDs and
relative dates to ISO datetimes. The LLM itself never handles IDs.

Config: MISTRAL_API_KEY, MISTRAL_MODEL (default mistral-small-latest).
"""
from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

from ..config import settings
from ..schema import Clarification, Card
from ..trello.client import TrelloLike
from .parser import WordParser

log = logging.getLogger(__name__)

API_URL = "https://api.mistral.ai/v1/chat/completions"

SYSTEM_PROMPT = """You manage the
team's cards on Trello. You are the FIRST to read every user message and
you own the conversation, which is free: greetings, small talk and general
questions are normal and are answered naturally. The user may write in any
language; reply in the same language, but the words you write for card
content (title, note) are ALWAYS in English.
A user message is free conversation OR a change on the Trello board OR an
enquiry about the board content.

You NEVER output JSON, code blocks or markup. You answer with plain words
only, and a parser in the program turns your words into the action.

The actions you may use are ONLY the ones in this catalogue
(actions_list.txt, loaded at runtime):

{actions}

For a board action, pick the action ONLY from that catalogue and put its
exact name on the action: line. Never invent or assume an action: if none of
them matches with certainty, do NOT guess - use one question: line instead.
Then write one labelled line per field, using these labels exactly:

  action: the exact name of one action from the catalogue above
  title:  the card title in English (for "add_card", if the user does not
          state one, write a short English title yourself)
  card:   which existing card you mean, as the user described it (only for
          "update card" / "mark done" / "move date" / "archive card")
  assign: the responsible people, comma separated, as the user named them
          (in "X must do Y" the subject X is ALWAYS the responsible person)
  due:    when: an ISO date or plain words like "tomorrow", "Friday",
          "next Tuesday at 9:00"
  list:   the column name as the user said it (To-do, Doing, Done, ...)
  note:   every other useful detail, in English (constraints, contact person
          with email and phone, location, times) - translate EVERY word to
          English, e.g. Italian "maestra" becomes "teacher"
  question: ONE short clarifying question, only when something is ambiguous

Rules:
- The five tools (board_cards, search_cards, get_card, list_members,
  list_lists) are READ-ONLY. Catalogue actions (add_card, update, complete,
  move_date, archive, add_list, query) are NEVER tools: never call a tool
  named "archive", "complete" or similar - write the action on the action:
  line in plain words instead.
- Give names exactly as the user said them (people, columns, cards). You do
  NOT resolve Trello IDs and never invent them - the parser resolves the
  names against the board.
- NEVER invent, guess or construct Trello IDs (24-char hex strings): you
  never need one. Put names on the labelled lines and call the read tools to
  see what exists. If a read tool returns an error, do NOT retry with
  made-up IDs - answer with a question: line or use the names you have.
- Whenever the user says a task is FOR someone or someone must do it
  ("la task è per Rob", "Rob deve farlo", "assign it to Rob"), ALWAYS put
  that person on the assign: line.
- No markdown: no **bold**, no # headers, no code blocks - plain words only.
- Use the tools (board_cards, list_members, list_lists, search_cards,
  get_card) to check which card, person or column the user means BEFORE
  answering. If several match or none does, use one question: line.
- "move X to Doing" / "mark X as done" / "sposta X in Doing": name the card
  on the card: line and the column on the list: line.
- For "update card" write only the fields that change (title, note, assign,
  due, list). To add a note to a card, put on the note: line the EXISTING
  description with the new note APPENDED (call get_card first) - never a
  paraphrase of the whole request, never a replacement.
- For pure conversation or general questions, answer with plain prose and NO
  action: line at all - your words become the chat reply.
Today is {today} ({weekday}, Europe/Berlin).
"""
# -- framing of the incoming user enquiries for a more effiient LLM interpretation
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
    {"type": "function", "function": {
        "name": "list_lists",
        "description": "List the board's columns/lists (id, name), e.g. To-do, "
                       "Doing, Done. Use this to resolve a column NAME the user "
                       "mentions to its real list ID for list_id.",
        "parameters": {"type": "object", "properties": {}}}},
]


class MistralLLM:
    MAX_HISTORY = 12                     # remembered turns per user

    def __init__(self, api_key: str | None = None,
                 model: str | None = None):
        self.api_key = api_key or settings.mistral_api_key
        self.model = model or settings.mistral_model
        self._history: dict[str, list[dict]] = {}   # per-user conversation
        self._parser = WordParser()   # words -> Card JSON (deterministic)

    # -- API plumbing ---------------------------------------------------------
    def _complete(self, messages: list[dict]) -> dict:
        # NOTE: response_format json_object must NOT be set here: combined
        # with tools it makes the model loop on tool calls without ever
        # emitting the final answer (observed on mistral-small-latest).
        # The final content is plain words; the parser fills the JSON.
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
        if name == "list_lists":
            return trello.list_lists()
        raise ValueError(f"unknown tool {name}")

    # -- LLMProvider protocol -----------------------------------------------------
    def interpret(self, text: str, user: dict, trello: TrelloLike) -> Card:
        tz = ZoneInfo(settings.timezone)
        today = datetime.now(tz)
        # free conversation: the user's previous turns are part of the
        # context, so greetings and follow-ups read naturally
        user_id = str(user.get("user_id", ""))
        history = self._history.setdefault(user_id, [])
        # the action catalogue (actions_list.txt) is part of the prompt: the
        # LLM may only choose actions from this list, never invent them
        actions_txt = "\n".join(
            f"- {a['name']}: {a.get('description', '')}"
            for a in self._parser.actions)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT.format(
                today=today.date().isoformat(),
                weekday=today.strftime("%A"),
                actions=actions_txt)},
            *history[-self.MAX_HISTORY:],
            {"role": "user", "content": text},
        ]

        unknown_tool: str | None = None
        last_tool_error: Exception | None = None
        for _ in range(5):                       # tool-use loop, bounded
            msg = self._complete(messages)
            calls = msg.get("tool_calls") or []
            if not calls:
                # remember the exchange for the next message
                history.append({"role": "user", "content": text})
                history.append({"role": "assistant",
                                "content": msg.get("content")})
                del history[:-self.MAX_HISTORY]
                # the LLM spoke in words; the parser fills the Card JSON
                # (raw_text = the user's original message, for the
                # deterministic assignee fallback on add_card)
                card = self._parser.parse(msg.get("content") or "", user,
                                          today, trello, raw_text=text)
                log.info("LLM words -> %s", card.action.value)
                return card
            messages.append(msg)
            for call in calls:
                fn = call["function"]
                args = json.loads(fn.get("arguments") or "{}")
                try:
                    content = json.dumps(
                        self._call_tool(fn["name"], args, trello))
                except ValueError as e:
                    # the model called something that is not a read tool
                    # (typically an action name like "archive"): answer as a
                    # tool error so the model self-corrects inside the loop
                    unknown_tool = fn["name"]
                    log.warning("LLM called unknown tool %r", fn["name"])
                    content = json.dumps({
                        "error": str(e),
                        "hint": "Catalogue actions (add_card, update, complete, "
                                "move_date, archive, add_list, query) are NOT "
                                "tools. Write the action on the action: line in "
                                "plain words. The only tools are the read-only: "
                                "board_cards, search_cards, get_card, "
                                "list_members, list_lists."})
                except Exception as e:
                    # a READ tool failed (e.g. get_card with a hallucinated
                    # ID -> HTTP 400/404): answer as a tool error so the
                    # model self-corrects instead of crashing the whole turn
                    last_tool_error = e
                    log.warning("tool %s failed: %r", fn["name"], e)
                    content = json.dumps({
                        "error": f"tool {fn['name']} failed: {e}",
                        "hint": "Do NOT retry with invented IDs. Use "
                                "board_cards / list_members / list_lists to "
                                "see what exists, or answer with a question: "
                                "line."})
                messages.append({
                    "role": "tool", "tool_call_id": call["id"],
                    "name": fn["name"], "content": content})
        if unknown_tool:
            # still failing after the bounded loop: tell the USER the problem
            # first, never a bare "something went wrong"
            raise Clarification(
                f'The tool "{unknown_tool}" is unknown, sorry. '
                "Please write something else.")
        if last_tool_error is not None:
            raise last_tool_error      # pipeline renders it problem-first
        raise Clarification("I could not resolve that. Can you rephrase?")

"""Orchestrator behind a Protocol (spec 2.1).

The LLM never emits JSON: it answers in plain words, and the deterministic
WordParser (parser.py) fills the Card JSON from those words. The only
implementation is MistralLLM (see mistral_llm.py, requires MISTRAL_API_KEY)
- the bot runs against the real Mistral LLM and the real Trello board.
"""
from __future__ import annotations

from typing import Protocol

from ..schema import Card
from ..trello.client import TrelloLike


class LLMProvider(Protocol):
    def interpret(self, text: str, user: dict,
                  trello: TrelloLike) -> Card: ...

"""Deterministic near-duplicate detection for task titles.

Message-ID idempotency (spec 5.2) cannot catch a request the user re-sends
with different wording: that is a new event. This module powers the
pipeline-level guard that warns about it (see Pipeline._similar_tasks).
Purely deterministic on purpose: no LLM involved (design principle).
"""
from __future__ import annotations

import difflib
import re

_THRESHOLD = 0.75


def normalize(title: str) -> str:
    """Casefold, drop punctuation, collapse whitespace."""
    t = re.sub(r"[^0-9a-z\u00c0-\u00ff]+", " ", title.casefold())
    return " ".join(t.split())


def similarity(a: str, b: str) -> float:
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def is_similar(a: str, b: str, threshold: float = _THRESHOLD) -> bool:
    return similarity(a, b) >= threshold

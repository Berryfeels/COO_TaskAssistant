"""Deterministic parser: the LLM's words -> the Card JSON (spec 2.1 step 4).

The LLM no longer emits JSON. It answers with plain words - one labelled line
per field (action:, title:, card:, assign:, due:, list:, note:, question:,
reply:) or free prose for conversation. WordParser is the program logic that
fills the JSON structure (the Card) from those words:

- action  <- the action: line, matched DETERMINISTICALLY against the action
  catalogue ./actions_list.txt (loaded at runtime, never hardcoded here).
  The match is a similarity score: >= 75% -> that action; below 75% (or two
  actions equally close) -> the user is asked to confirm, never guessed.
  No action: line at all -> free conversation (chat)
- title   <- the title: line, else the first quoted string in the text
- card    <- the card: line, resolved against the open board cards
- assign  <- names as the user said them, resolved to member IDs
- list    <- the column name as the user said it, resolved to its idList
- due     <- ISO or plain words ("tomorrow", "next Friday at 9:00"),
  resolved in Europe/Berlin
- question<- asked back to the user via Clarification instead of guessing

Everything here is deterministic: the action comes only from actions_list.txt,
names resolve against the board with fuzzy matching, dates resolve with
app.dates, and anything ambiguous raises Clarification with ONE question
listing the candidates (ambiguity policy, spec section 6). The LLM never
invents actions and never handles Trello IDs.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ..config import settings
from ..dates import resolve_relative
from ..schema import Action, Card, Clarification, parse_action
from ..similarity import is_similar, similarity
from ..trello.client import TrelloLike

TZ = ZoneInfo(settings.timezone)

# The action catalogue: repo-root actions_list.txt (override: ACTIONS_LIST_PATH)
ACTIONS_FILE = Path(os.environ.get(
    "ACTIONS_LIST_PATH",
    str(Path(__file__).resolve().parents[3] / "actions_list.txt")))


def load_actions(path: str | Path | None = None) -> list[dict]:
    """Load and validate the action catalogue. Every entry's name must map
    to a known Action (parse_action also accepts the legacy "create")."""
    p = Path(path) if path is not None else ACTIONS_FILE
    data = json.loads(p.read_text(encoding="utf-8"))
    actions = []
    for entry in data:
        parse_action(entry.get("name", ""))      # raises ValueError if unknown
        entry.setdefault("triggers", [])
        actions.append(entry)
    return actions

# label on a line -> internal field ("title: X" / "titolo: X" -> title)
_LABELS = {
    "action": "action", "do": "action", "azione": "action",
    "title": "title", "titolo": "title",
    "card": "card", "target": "card", "scheda": "card",
    "assign": "assign", "assignee": "assign", "assigned to": "assign",
    "assign to": "assign", "assegna": "assign", "member": "assign",
    "due": "due", "when": "due", "deadline": "due", "scadenza": "due",
    "list": "list", "column": "list", "colonna": "list", "lista": "list",
    "note": "note", "description": "note", "details": "note", "nota": "note",
    "question": "question", "ask": "question", "domanda": "question",
    "reply": "reply", "rispondi": "reply",
}

_LINE = re.compile(r"^\s*[-*•]?\s*([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ ]{0,24}?)\s*:\s*(.+?)\s*$")
_QUOTED = re.compile(r"[\"'“”‘’](.+?)[\"'“”‘’]")

_WEEKDAYS_IT = {"lunedi": "monday", "martedi": "tuesday", "mercoledi": "wednesday",
                "giovedi": "thursday", "venerdi": "friday", "sabato": "saturday",
                "domenica": "sunday"}
_MONTHS = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
           "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
           "november": 11, "december": 12, "jan": 1, "feb": 2, "mar": 3,
           "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
           "oct": 10, "nov": 11, "dec": 12,
           "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4,
           "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
           "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12}

_FILLER = re.compile(r"\b(at|on|the|for|by|il|la|lo|di|alle|al)\b", re.I)


def _deaccent(w: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", w)
                   if unicodedata.category(c) != "Mn")


def _norm(s: str) -> str:
    """Casefold + strip accents + strip non-word chars (emoji, punctuation)."""
    s = _deaccent(s.casefold())
    return " ".join(re.sub(r"[^\w\s]", " ", s).split())


def _first_quoted(text: str) -> str:
    m = _QUOTED.search(text)
    return m.group(1).strip() if m else ""


def _split_names(value: str) -> list[str]:
    parts = re.split(r",|\band\b|\be\b", value, flags=re.I)
    return [p.strip() for p in parts if p.strip()]


class WordParser:
    """Fills the Card JSON from the LLM's plain words. Deterministic."""

    MATCH_THRESHOLD = 0.75      # action relation must reach +75%, else ask

    def __init__(self, actions: list[dict] | None = None):
        self.actions = actions if actions is not None else load_actions()

    # -- line splitting ---------------------------------------------------------
    def _split_fields(self, words: str) -> tuple[dict[str, str], list[str]]:
        fields: dict[str, str] = {}
        free: list[str] = []
        for line in words.splitlines():
            m = _LINE.match(line)
            if m:
                label = _LABELS.get(m.group(1).strip().casefold())
                if label:
                    fields[label] = m.group(2)
                    continue
            if line.strip():
                free.append(line.strip())
        return fields, free

    # -- action detection: ONLY from the actions_list.txt catalogue ---------------
    def _rank_actions(self, text: str) -> list[tuple[float, int, Action]]:
        """Deterministic ranking of the action: words against the catalogue.
        Each entry scores the max of the similarity against its
        name/description/triggers; a trigger phrase found verbatim in the
        text scores 1.0. Sorted by (score, longest matching trigger): the
        most specific phrase wins ties."""
        t = _norm(text)
        ranked = []
        if t:
            for entry in self.actions:
                name = _norm(entry.get("name", ""))
                score = similarity(t, name) if name else 0.0
                trig_len = 0
                score = max(score, similarity(t, _norm(entry.get("description", ""))))
                for tr in entry.get("triggers", []):
                    tn = _norm(tr)
                    if not tn:
                        continue
                    if tn in t:
                        if len(tn) >= trig_len:
                            score, trig_len = max(score, 1.0), len(tn)
                    else:
                        score = max(score, similarity(t, tn))
                ranked.append((score, trig_len, parse_action(entry["name"])))
        ranked.sort(key=lambda r: (r[0], r[1]), reverse=True)
        return ranked

    def _match_action(self, text: str) -> tuple[Action | None, float, float]:
        """(best_action, best_score, second_score) for the action: words."""
        ranked = self._rank_actions(text)
        if not ranked:
            return None, 0.0, 0.0
        second = ranked[1] if len(ranked) > 1 else (0.0, 0, None)
        return ranked[0][2], ranked[0][0], second[0]

    def _decide_action(self, text: str) -> Action:
        """The one action the action: words relate to, or a Clarification
        asking the user to confirm. >= 75% proceeds; below, or two actions
        equally close without a more specific trigger, asks - never guesses."""
        ranked = self._rank_actions(text)
        if not ranked:
            raise Clarification(self._action_question(None, 0.0))
        best = ranked[0]
        second = ranked[1] if len(ranked) > 1 else None
        if best[0] < self.MATCH_THRESHOLD:
            raise Clarification(self._action_question(best[2], best[0]))
        if second and second[0] >= self.MATCH_THRESHOLD \
                and best[0] - second[0] < 0.1 and second[1] >= best[1]:
            raise Clarification(self._action_question(best[2], best[0]))
        return best[2]

    def _action_question(self, best: Action | None, score: float) -> str:
        """Below-threshold or ambiguous match: ask the USER to confirm which
        action is meant, listing the catalogue. Never guess."""
        opts = "\n".join(f"- {a['name']}: {a.get('description', '')}"
                         for a in self.actions)
        if best is not None:
            head = (f"I am not sure which action you mean "
                    f"(best match '{best.value}', {score:.0%}): "
                    f"do you confirm '{best.value}'?")
        else:
            head = "I could not relate that to any action."
        return head + "\nAvailable actions:\n" + opts + "\nWhich one do you mean?"

    # -- name -> ID resolution (deterministic, against the board) -----------------
    def _resolve_member(self, name: str, members: list[dict]) -> str:
        n = _norm(name)
        for m in members:
            if n and n == _norm(m.get("fullName", "")):
                return m.get("id", "")
        for m in members:
            tokens = _norm(m.get("fullName", "")).split()
            if tokens and (n == tokens[0] or n in tokens):
                return m.get("id", "")
        # prefix / fuzzy first-name match: "rob" -> "Robert"
        for m in members:
            tokens = _norm(m.get("fullName", "")).split()
            if tokens and ((len(n) >= 3 and tokens[0].startswith(n))
                           or (len(n) >= 4 and is_similar(tokens[0], n))):
                return m.get("id", "")
        available = ", ".join(m.get("fullName", "?") for m in members) or "none"
        raise Clarification(f"'{name}' is not a member of this board. "
                            f"Members: {available}")

    def _members_in_text(self, text: str, members: list[dict]) -> list[dict]:
        """Board members mentioned in the raw user text (deterministic):
        full name, any name token, a >=3-char prefix of the first token
        ("rob" -> "Robert") or a fuzzy first-token match."""
        t = _norm(text)
        tokens = set(t.split())
        found: list[dict] = []
        for m in members:
            name = _norm(m.get("fullName", ""))
            ntokens = name.split()
            if not ntokens:
                continue
            first = ntokens[0]
            hit = (name in t) or any(tok in ntokens for tok in tokens) \
                or any(len(tok) >= 3 and first.startswith(tok) for tok in tokens) \
                or any(len(tok) >= 4 and is_similar(first, tok) for tok in tokens)
            if hit and m not in found:
                found.append(m)
        return found

    def _resolve_list(self, name: str, lists: list[dict]) -> str:
        n = _norm(name)
        for lst in lists:
            ln = _norm(lst.get("name", ""))
            if n and (n == ln or n in ln or ln in n):
                return lst.get("id", "")
        available = ", ".join(l.get("name", "?") for l in lists) or "none"
        raise Clarification(f"There is no column called '{name}'. "
                            f"Available: {available}")

    def _resolve_card(self, name: str, trello: TrelloLike) -> str:
        open_cards = [c for c in trello.board_cards() if not c.get("closed")]
        if not open_cards:
            raise Clarification("There are no open cards on the board.")
        scored = sorted(
            ((similarity(name, c.get("name") or ""), c) for c in open_cards),
            key=lambda pair: pair[0], reverse=True)
        above = [(s, c) for s, c in scored if s >= 0.6]
        if not above:
            names = ", ".join(f"'{c.get('name')}'" for _, c in scored[:5])
            raise Clarification(f"I could not find a card like '{name}'. "
                                f"Open cards: {names}")
        if len(above) > 1 and above[0][0] - above[1][0] < 0.1:
            names = ", ".join(f"'{c.get('name')}'" for _, c in above[:3])
            raise Clarification(f"Which card do you mean? {names}")
        return above[0][1].get("id", "")

    # -- date resolution ------------------------------------------------------------
    def _parse_due(self, value: str, today: datetime) -> str:
        v = value.strip()
        try:                                    # ISO 8601 passthrough
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ)
            return dt.isoformat()
        except ValueError:
            pass
        t = v.casefold()
        hour = minute = None
        m = re.search(r"\b(\d{1,2})[:.](\d{2})\b", t)          # 8:00 / 8.00
        if m:
            hour, minute = int(m.group(1)), int(m.group(2))
            t = t[:m.start()] + t[m.end():]
        else:
            m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", t)        # 8am / 8 pm
            if m:
                hour = int(m.group(1)) % 12 + (12 if m.group(2) == "pm" else 0)
                minute = 0
                t = t[:m.start()] + t[m.end():]
            else:
                m = re.search(r"\bat\s+(\d{1,2})\b", t)         # at 8
                if m:
                    hour, minute = int(m.group(1)), 0
                    t = t[:m.start()] + t[m.end():]
        try:
            if (m := re.search(r"\bin\s+(\d+)\s+days?\b", t)):
                day = today + timedelta(days=int(m.group(1)))
                return self._finish_due(day, hour, minute)
            rest = _FILLER.sub(" ", t)
            rest = " ".join(_WEEKDAYS_IT.get(_deaccent(w), _deaccent(w))
                            for w in rest.split()).strip(" ,.")
            if not rest:
                day = today
            elif (m := re.search(r"\b(\d{1,2})\s+([a-z]+)\b", rest)) \
                    and m.group(2) in _MONTHS:                  # 17 november
                month, day_n = _MONTHS[m.group(2)], int(m.group(1))
                try:
                    day = today.replace(year=today.year, month=month,
                                        day=day_n)
                except ValueError:
                    raise ValueError(f"bad date {value!r}")
                if day.date() < today.date():
                    day = day.replace(year=today.year + 1)
            else:
                day = resolve_relative(today, rest)
        except ValueError:
            raise Clarification(
                f"I did not understand the date '{value}'. "
                "Use e.g. 'tomorrow', 'Friday', 'next Tuesday at 9:00' "
                "or an ISO date.")
        return self._finish_due(day, hour, minute)

    @staticmethod
    def _finish_due(day: datetime, hour: int | None,
                    minute: int | None) -> str:
        if hour is None:
            return day.date().isoformat()                        # date only
        day = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if day.tzinfo is None:
            day = day.replace(tzinfo=TZ)
        return day.isoformat()

    # -- main entry point -----------------------------------------------------------
    def parse(self, words: str, user: dict, today: datetime,
              trello: TrelloLike, raw_text: str = "") -> Card:
        """Turn the LLM's plain words into the Card JSON. Raises
        Clarification (one question) whenever something is ambiguous.
        `raw_text` is the user's original message: used for the
        deterministic assignee fallback on add_card."""
        text = (words or "").strip()
        if not text:
            raise Clarification("I did not understand. Can you rephrase?")
        fields, _free = self._split_fields(text)

        if fields.get("question"):
            raise Clarification(fields["question"])

        # The action comes ONLY from the action: line, verified against the
        # actions_list.txt catalogue. Below 75% (or ambiguous) the user is
        # asked to confirm - the parser never invents or assumes an action.
        # No action: line -> free conversation (chat).
        action = None
        if fields.get("action"):
            action = self._decide_action(fields["action"])
        source = str(user.get("source_message_id", ""))

        # free conversation: the LLM's words ARE the chat reply
        if action is None or action == Action.chat:
            return Card(action=Action.chat, reply=fields.get("reply") or text,
                        source_message_id=source)
        if action == Action.query:
            return Card(action=Action.query, source_message_id=source)

        title = fields.get("title") or _first_quoted(text)
        card = Card(action=action, title=title,
                    description=fields.get("note", ""),
                    source_message_id=source)

        if action == Action.add_card:
            if not card.title:
                raise Clarification("What should the new card be called?")
        elif action == Action.add_list:
            if not card.title:
                card.title = "new List"      # same default as Card.validate
        else:
            target = fields.get("card") or _first_quoted(text)
            if not target:
                open_names = ", ".join(
                    f"'{c.get('name')}'" for c in trello.board_cards()
                    if not c.get("closed")) or "none"
                raise Clarification(f"Which card do you mean? Open cards: "
                                    f"{open_names}")
            card.target_card_id = self._resolve_card(target, trello)
            if action == Action.complete:
                card.done = True
            if action == Action.archive:
                card.closed = True

        if fields.get("assign"):
            members = trello.list_members()
            card.assignees = [self._resolve_member(n, members)
                              for n in _split_names(fields["assign"])]
        elif action == Action.add_card and raw_text:
            # deterministic safety net: the LLM may put "for Rob" / "per Rob"
            # in prose instead of an assign: line - a new task that names a
            # board member must never be created unassigned
            mentioned = self._members_in_text(raw_text, trello.list_members())
            if len(mentioned) == 1:
                card.assignees = [mentioned[0].get("id", "")]
            elif len(mentioned) > 1:
                names = ", ".join(m.get("fullName", "?") for m in mentioned)
                raise Clarification(
                    f"Who should this card be assigned to? "
                    f"Members mentioned: {names}")

        if fields.get("list"):
            card.list_id = self._resolve_list(fields["list"],
                                              trello.list_lists())
        elif action == Action.update and not card.list_id:
            # "move card to Doing" without a list: line -> parse "to X"
            m = re.search(r"\b(?:to|in|into|nella)\s+([A-Za-zÀ-ÿ ]+?)\s*$",
                          fields.get("action", ""), re.I)
            if m:
                card.list_id = self._resolve_list(m.group(1),
                                                  trello.list_lists())
        elif action == Action.complete:
            done = next((l.get("id", "") for l in trello.list_lists()
                         if "done" in _norm(l.get("name", ""))), None)
            if done:
                card.list_id = done          # "done" lands in the Done column

        if fields.get("due"):
            card.due = self._parse_due(fields["due"], today)
        elif action == Action.move_date:
            raise Clarification("What is the new due date?")

        return card

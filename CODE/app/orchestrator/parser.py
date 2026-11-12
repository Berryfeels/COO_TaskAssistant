"""Deterministic parser: the LLM's words -> the Card JSON (spec 2.1 step 4).

The LLM no longer emits JSON. It answers with plain words - one labelled line
per field (action:, title:, card:, assign:, due:, list:, note:, question:,
reply:) or free prose for conversation. WordParser is the program logic that
fills the JSON structure (the Card) from those words:

- action  <- the action: line (controlled vocabulary), else keywords in the
  free text, else chat
- title   <- the title: line, else the first quoted string in the text
- card    <- the card: line, resolved against the open board cards
- assign  <- names as the user said them, resolved to member IDs
- list    <- the column name as the user said it, resolved to its idList
- due     <- ISO or plain words ("tomorrow", "next Friday at 9:00"),
  resolved in Europe/Berlin
- question<- asked back to the user via Clarification instead of guessing

Everything here is deterministic: names resolve against the board with fuzzy
matching, dates resolve with app.dates, and anything ambiguous raises
Clarification with ONE question listing the candidates (ambiguity policy,
spec section 6). The LLM never handles Trello IDs.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..config import settings
from ..dates import resolve_relative
from ..schema import Action, Card, Clarification
from ..similarity import similarity
from ..trello.client import TrelloLike

TZ = ZoneInfo(settings.timezone)

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

    # -- action detection ---------------------------------------------------------
    def _action(self, action_value: str, text: str) -> Action | None:
        """Action from the action: line; if absent/unrecognized, keywords in
        the free text; None means free conversation (chat)."""
        for source in (action_value, text if not action_value else ""):
            if not source:
                continue
            a = self._action_from_text(source)
            if a is not None:
                return a
        return None

    @staticmethod
    def _action_from_text(t: str) -> Action | None:
        t = t.casefold()
        if re.search(r"\b(new|add|create|aggiungi|crea)\b", t) and \
                re.search(r"\b(column|colonna|list|lista)\b", t):
            return Action.add_list
        if re.search(r"\b(move|sposta|cambia|reschedul|postpone)\b", t) and \
                re.search(r"\b(date|due|deadline|scadenza|data)\b", t):
            return Action.move_date
        if re.search(r"\bmetti\b[^.\n]*\b(done|fatto|complet\w*)\b", t):
            return Action.complete                    # "metti X in fatto"
        if re.search(r"\b(move|sposta)\b", t):
            return Action.update                      # move to a column
        if re.search(r"\b(archiv\w*|delete|elimina\w*|close)\b", t):
            return Action.archive
        if re.search(r"\b(mark\w*|set\w*)\b[^.\n]*\bdone\b", t) or \
                re.search(r"\b(done|complet\w*|fatto|finisc\w*)\b", t):
            return Action.complete
        if re.search(r"\b(add|create|new|aggiungi|crea|note\s*down)\b[^.\n]*"
                     r"\b(card|task|to-?do|reminder|scheda)\b", t) or \
                re.search(r"\b(aggiungi|crea)\b", t):
            return Action.add_card
        if re.search(r"\b(show|list|what|which|quali|cosa|how many)\b[^.\n]*"
                     r"\b(cards?|tasks?|schede|to-?dos?)\b", t) or \
                re.search(r"\bmy\s+cards\b", t):
            return Action.query
        if re.search(r"\b(update|change|edit|rename|modifica\w*)\b", t):
            return Action.update
        if re.search(r"\b(chat|talk|reply|answer|greet\w*)\b", t):
            return Action.chat
        return None

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
        available = ", ".join(m.get("fullName", "?") for m in members) or "none"
        raise Clarification(f"'{name}' is not a member of this board. "
                            f"Members: {available}")

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
              trello: TrelloLike) -> Card:
        """Turn the LLM's plain words into the Card JSON. Raises
        Clarification (one question) whenever something is ambiguous."""
        text = (words or "").strip()
        if not text:
            raise Clarification("I did not understand. Can you rephrase?")
        fields, _free = self._split_fields(text)

        if fields.get("question"):
            raise Clarification(fields["question"])

        action = self._action(fields.get("action", ""), text)
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

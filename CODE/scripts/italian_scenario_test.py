#!/usr/bin/env python3
"""Conversational test: rich Italian task creation (real Mistral + Trello).

Usage:
    export MISTRAL_API_KEY=...             # required
    export TRELLO_KEY=... TRELLO_TOKEN=...
    export TRELLO_SANDBOX_BOARD_ID=...     # sandbox board id
    export TRELLO_SANDBOX_LIST_ID=...      # list (column) where test cards land
    export MISTRAL_MODEL=...               # optional, default mistral-small-latest

    python3 scripts/italian_scenario_test.py

Message under test (Italian, rich):

    "Stefano deve fare una torta per martedi 17 Novembre alla entrata di
    scuola alle 8.00. La torta deve evitare latticini. il contatto è la
    maestra Carla (info@carla.com, tel: 809854544). Gli studenti mangiano
    alle 11.30"

Checks the field-distribution policy:
- any input language; card content (title, description) in English
- title auto-generated in English when not explicitly stated
- subject "Stefano" is the responsible member (assignee)
- date + time -> due: Tuesday 2026-11-17 08:00, Europe/Berlin
- comments (dairy-free, contact Carla, students eat at 11:30,
  school entrance) -> description, in English

The card is created on the sandbox board after confirmation and is NOT
deleted: it stays on the board for review in the Trello dashboard (the
script prints its URL; delete it manually when done).
"""
import asyncio
import json
import os
import sys
import urllib.error
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from mistral_conversation_test import (USER_ID, PrintingGateway, SandboxListLLM,
                                       _mid, _new_cards)
from app.config import settings
from app.confirm.pending import PendingService
from app.orchestrator.mistral_llm import MistralLLM
from app.pipeline import Pipeline
from app.schema import Action, Task
from app.storage.db import DB
from app.trello.client import TrelloClient

PASS, FAIL = "\033[92mPASS\033[0m", "\033[91mFAIL\033[0m"

SCENARIO_MSG = ("Stefano deve fare una torta per martedi 17 Novembre alla "
                "entrata di scuola alle 8.00. La torta deve evitare "
                "latticini. il contatto è la maestra Carla "
                "(info@carla.com, tel: 809854544). Gli studenti mangiano "
                "alle 11.30")

# Facts the message states; the card must carry them (in English).
DUE_DATE = (2026, 11, 17)               # Tuesday
DUE_TIME = 8                            # 08:00 Europe/Berlin
DESC_MUST_CONTAIN = ["dairy", "carla", "info@carla.com", "809854544",
                     "school", "11:30"]
DESC_MUST_NOT_CONTAIN = ["torta", "latticini", "maestra"]


def check(name, cond, detail=""):
    print(f"  [{PASS if cond else FAIL}] {name}"
          + (f" -- {detail}" if not cond and detail else ""))
    return bool(cond)


class SandboxHarness(SandboxListLLM):
    """Sandbox wrapper: force the sandbox list and drop labels (the sandbox
    harness cannot resolve label IDs; the policy forbids inventing them)."""

    def interpret(self, text: str, user: dict, trello) -> Task:
        task = super().interpret(text, user, trello)
        task.labels = []
        return task


async def run(pipeline, db, trello, gateway, pre_ids: set) -> list[bool]:
    results: list[bool] = []
    task, pending_id = None, None

    print("1) user message (Italian):")
    print(f"   {SCENARIO_MSG!r}")
    await pipeline.handle_incoming(USER_ID, _mid(), SCENARIO_MSG)
    active = db.pending_active(USER_ID)
    if not active:
        print(f"  [info] no pending action, assistant said: "
              f"{gateway.replies[-1]!r}")
        return [check("LLM produced a create action", False)]
    row = active[-1]
    task, pending_id = row.task, row.pending_id

    print("2) extracted Task (orchestrator output, spec section 4):")
    print("   " + json.dumps(task.__dict__, default=str, indent=2)
          .replace("\n", "\n   "))

    results.append(check("LLM produced a create action",
                         task.action == Action.create,
                         f"action={task.action}"))
    results.append(check("title in English, auto-generated from the request",
                         "cake" in task.title.lower()
                         and "torta" not in task.title.lower(),
                         repr(task.title)))
    members = {m["id"]: m["fullName"] for m in trello.list_members()}
    stefano = [mid for mid, name in members.items()
               if "stefano" in name.lower()]
    results.append(check("subject 'Stefano' is the responsible member",
                         sorted(task.assignees) == sorted(stefano),
                         f"assignees={task.assignees} vs members={members}"))
    due_dt = None
    try:
        due_dt = datetime.fromisoformat((task.due or "").replace("Z", "+00:00"))
    except ValueError:
        pass
    results.append(check("due: date+time with Europe/Berlin offset",
                         due_dt is not None
                         and (due_dt.year, due_dt.month, due_dt.day) == DUE_DATE
                         and due_dt.hour == DUE_TIME
                         and due_dt.utcoffset() == timedelta(hours=1),
                         f"due={task.due!r}"))
    desc = (task.description or "").lower()
    missing = [s for s in DESC_MUST_CONTAIN if s not in desc]
    results.append(check("description carries every stated fact (English)",
                         not missing, f"missing: {missing}, desc={desc!r}"))
    italian = [s for s in DESC_MUST_NOT_CONTAIN if s in desc]
    results.append(check("description is English, not Italian",
                         not italian, f"found Italian words: {italian}"))
    results.append(check("confirmation requested with pending id",
                         f"OK {pending_id}" in gateway.replies[-1]))
    if not results[-1]:
        return results

    print(f"3) user reply: 'OK {pending_id}'")
    await pipeline.handle_incoming(USER_ID, _mid(), f"OK {pending_id}")
    results.append(check("apply acknowledged",
                         gateway.replies[-1].startswith("Done:"),
                         repr(gateway.replies[-1])))

    print("4) mirror audit: card on the sandbox board")
    new = _new_cards(trello, pre_ids)
    card = next((c for c in new if c.get("name") == task.title),
                new[0] if new else None)
    results.append(check("card visible on board immediately", card is not None))
    if card:
        print(f"   card {card['id']}: {card.get('name')!r} "
              f"due={card.get('due')!r} idList={card.get('idList')!r}")
        back = Task.from_card(card)
        results.append(check("round-trip title", back.title == task.title,
                             f"{back.title!r} != {task.title!r}"))
        results.append(check("round-trip description",
                             back.description == task.description,
                             f"{back.description!r} != {task.description!r}"))
        results.append(check("assignee mirrored on the card",
                             sorted(back.assignees) == sorted(task.assignees),
                             f"{back.assignees} != {task.assignees}"))
        results.append(check("due mirrored on the card (Berlin time kept)",
                             bool(card.get("due"))
                             and datetime.fromisoformat(
                                 card["due"].replace("Z", "+00:00")
                             ).hour == DUE_TIME - 1,
                             f"card due={card.get('due')!r}"))
    return results


def main() -> int:
    key = os.environ.get("TRELLO_KEY", "")
    token = os.environ.get("TRELLO_TOKEN", "")
    board_id = os.environ.get("TRELLO_SANDBOX_BOARD_ID", "")
    list_id = os.environ.get("TRELLO_SANDBOX_LIST_ID", "")
    api_key = os.environ.get("MISTRAL_API_KEY", "") or settings.mistral_api_key
    missing = [n for n, v in {"MISTRAL_API_KEY": api_key, "TRELLO_KEY": key,
                              "TRELLO_TOKEN": token,
                              "TRELLO_SANDBOX_BOARD_ID": board_id,
                              "TRELLO_SANDBOX_LIST_ID": list_id}.items()
               if not v]
    if missing:
        print(__doc__)
        print(f"missing environment variables: {', '.join(missing)}")
        return 2

    db = DB(":memory:")                   # throwaway: never touches coo.db
    trello = TrelloClient(key, token, board_id=board_id)
    llm = MistralLLM(model=os.environ.get("MISTRAL_MODEL")
                     or settings.mistral_model)
    pending = PendingService(db, settings)
    gateway = PrintingGateway()
    pipeline = Pipeline(db, trello, SandboxHarness(llm, list_id),
                        pending, gateway, settings)
    try:
        pre_ids = {c["id"] for c in trello.board_cards()}
    except urllib.error.HTTPError as e:
        print(f"Trello error while reading the board: {e.code} {e.reason}")
        print(e.read().decode(errors="replace"))
        return 2

    try:
        results = asyncio.run(run(pipeline, db, trello, gateway, pre_ids))
        ok = sum(results)
        print(f"\n{ok}/{len(results)} checks passed.")
        return 0 if ok == len(results) else 1
    except urllib.error.HTTPError as e:
        src = "Mistral" if "api.mistral.ai" in str(e.url) else "Trello"
        print(f"\n{src} API error: {e.code} {e.reason}")
        print(e.read().decode(errors="replace"))
        return 2
    finally:
        kept = _new_cards(trello, pre_ids)
        if kept:
            print(f"\nkept {len(kept)} test card(s) on the board "
                  f"for review in the Trello dashboard:")
            for c in kept:
                full = trello.get_card(c["id"])
                print(f"  - {c['id']}  {c.get('name')!r}  "
                      f"{full.get('shortUrl', '')}")


if __name__ == "__main__":
    sys.exit(main())

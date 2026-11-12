#!/usr/bin/env python3
"""Conversational test for build-order phase 2: the real MistralLLM
interpreting natural-language messages, full pipeline in between
(message -> confirmation -> card created on the Trello SANDBOX board).

Usage:
    export MISTRAL_API_KEY=...             # required
    export TRELLO_KEY=...
    export TRELLO_TOKEN=...
    export TRELLO_BOARD_SANDBOX_ID=...     # sandbox board id
    export TRELLO_DEFAULT_LIST_ID=...      # list (column) where test cards land
                                            # (both are read from .env if not exported)
    export MISTRAL_MODEL=...               # optional, default mistral-small-latest

    python3 scripts/mistral_conversation_test.py            # scripted scenario
    python3 scripts/mistral_conversation_test.py -i        # free conversation (REPL)
    python3 scripts/mistral_conversation_test.py --keep    # keep the test cards

The scripted scenario sends "create card ... for Marco due Friday", prints the
Card the LLM extracted, asks for a confirmation, replies "OK <pending_id>",
verifies the card on the board (mirror audit) and finally deletes the cards
it created (unless --keep). Only cards created during this run are touched.
If 'Marco' is not a member of the sandbox board the LLM is expected to ask a
clarifying question; the scenario then retries without the assignee.
"""
import argparse
import asyncio
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from run_bot import load_dotenv
load_dotenv()

from app.config import settings
from app.confirm.pending import PendingService
from app.orchestrator.mistral_llm import MistralLLM
from app.pipeline import Gateway, Pipeline
from app.schema import Action, Card
from app.storage.db import DB
from app.trello.client import TrelloClient

PASS, FAIL = "\033[92mPASS\033[0m", "\033[91mFAIL\033[0m"
USER_ID = "sandbox-user"
SCENARIO_MSG = "create card Call the supplier for Marco due Friday"
SCENARIO_FALLBACK = "create card Call the supplier due Friday"


def check(name, cond, detail=""):
    print(f"  [{PASS if cond else FAIL}] {name}"
          + (f" -- {detail}" if not cond and detail else ""))
    return bool(cond)


def _mid() -> str:
    return "msg-" + uuid.uuid4().hex[:8]


class PrintingGateway(Gateway):
    """Terminal stand-in for the SimpleX gateway: prints replies, keeps them."""

    def __init__(self):
        super().__init__()
        self.replies: list[str] = []

    async def send(self, user_id: str, text: str) -> None:
        await super().send(user_id, text)
        self.replies.append(text)
        print(f"\n  ASSISTANT> {text}\n")


class SandboxListLLM:
    """Wraps the LLM so create actions land in the sandbox list when the
    model does not (and cannot) pick a column itself."""

    def __init__(self, inner, list_id: str):
        self.inner, self.list_id = inner, list_id

    def interpret(self, text: str, user: dict, trello) -> Card:
        card = self.inner.interpret(text, user, trello)
        if card.action == Action.add_card and not card.list_id:
            card.list_id = self.list_id
        return card


def build(board_id: str, list_id: str, key: str, token: str):
    db = DB(":memory:")                     # throwaway: never touches coo.db
    trello = TrelloClient(key, token, board_id=board_id)
    llm = MistralLLM(model=os.environ.get("MISTRAL_MODEL")
                      or settings.mistral_model)
    pending = PendingService(db, settings)
    gateway = PrintingGateway()
    pipeline = Pipeline(db, trello, SandboxListLLM(llm, list_id),
                        pending, gateway, settings)
    return db, trello, pipeline, gateway


def _new_cards(trello, pre_ids: set) -> list[dict]:
    return [c for c in trello.board_cards() if c["id"] not in pre_ids]


def cleanup(trello, pre_ids: set, key: str, token: str, keep: bool) -> None:
    new = _new_cards(trello, pre_ids)
    if not new:
        return
    if keep:
        print("cleanup: keeping the test cards on the board:")
        for c in new:
            print(f"  - {c['id']}  {c.get('name')!r}")
        return
    print(f"cleanup: deleting {len(new)} test card(s) created by this run")
    for c in new:
        url = (f"{trello.BASE}/cards/{c['id']}?"
               + urllib.parse.urlencode({"key": key, "token": token}))
        req = urllib.request.Request(url, method="DELETE")
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    print("  deleted.")


# -- scripted scenario -------------------------------------------------------
async def run_scenario(pipeline, db, trello, gateway, pre_ids: set,
                       message: str, fallback: str | None) -> list[bool]:
    results: list[bool] = []
    card, pending_id = None, None

    attempts = [message] + ([fallback] if fallback else [])
    for i, text in enumerate(attempts):
        print(f"1.{i + 1}) user message: {text!r}")
        await pipeline.handle_incoming(USER_ID, _mid(), text)
        active = db.pending_active(USER_ID)
        if active:
            row = active[-1]
            card, pending_id = row.card, row.pending_id
            break
        print(f"  [info] no pending action, assistant said: "
              f"{gateway.replies[-1]!r}")

    results.append(check("LLM produced a create action",
                         card is not None and card.action == Action.add_card,
                         f"action={getattr(card, 'action', None)}"))
    if card is None or card.action != Action.add_card:
        return results

    print("2) extracted Card (orchestrator output, spec section 4):")
    print("   " + json.dumps(card.__dict__, default=str, indent=2)
          .replace("\n", "\n   "))
    results.append(check("title extracted", bool(card.title), repr(card.title)))
    results.append(check("create lands in the sandbox list",
                         card.list_id == pipeline.llm.list_id,
                         f"list_id={card.list_id!r}"))
    if "friday" in message.lower():
        due_dt = None
        try:
            due_dt = datetime.fromisoformat(
                (card.due or "").replace("Z", "+00:00"))
        except ValueError:
            pass
        results.append(check("due resolved to a Friday",
                             due_dt is not None and due_dt.weekday() == 4,
                             f"due={card.due!r}"))
    if card.assignees:
        members = {m["id"]: m["fullName"] for m in trello.list_members()}
        ids_ok = all(a in members for a in card.assignees)
        results.append(check("assignees are real board members", ids_ok,
                             f"{card.assignees} vs {members}"))
        if ids_ok:
            print(f"   assignee(s): "
                  + ", ".join(members[a] for a in card.assignees))

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
    found = next((c for c in new if c.get("name") == card.title),
                 new[0] if new else None)
    results.append(check("card visible on board immediately (deterministic read)",
                         found is not None))
    if found:
        print(f"   card {found['id']}: {found.get('name')!r} "
              f"due={found.get('due')!r} idList={found.get('idList')!r}")
        back = Card.from_api(found)
        results.append(check("round-trip title", back.title == card.title,
                             f"{back.title!r} != {card.title!r}"))
        results.append(check("round-trip list",
                             back.list_id == card.list_id,
                             f"{back.list_id!r} != {card.list_id!r}"))
        if card.assignees:
            results.append(check("assignee mirrored on the card",
                                sorted(back.assignees) == sorted(card.assignees),
                                f"{back.assignees} != {card.assignees}"))
        if card.due:
            results.append(check("due mirrored on the card",
                                 bool(found.get("due"))))
    return results


# -- interactive REPL ----------------------------------------------------------
async def run_interactive(pipeline, db) -> None:
    print("Interactive conversation against the sandbox board.")
    print("Type a message; reply \"OK <pending_id>\" to confirm a write.")
    print("Commands: /pending (list pending actions), /quit.\n")
    n = 0
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text in ("/quit", "/exit"):
            break
        if text == "/pending":
            active = db.pending_active(USER_ID)
            if not active:
                print("  (no pending actions)")
            for p in active:
                print(f"  {p.pending_id}: {p.card.action.value} "
                      f"{p.card.title or p.card.target_card_id!r}")
            continue
        n += 1
        await pipeline.handle_incoming(USER_ID, _mid(), text)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Conversational test: MistralLLM + pipeline + sandbox board")
    ap.add_argument("-i", "--interactive", action="store_true",
                    help="free conversation instead of the scripted scenario")
    ap.add_argument("--keep", action="store_true",
                    help="do not delete the cards created during the run")
    ap.add_argument("--message", default=None,
                    help="override the scripted create message")
    args = ap.parse_args()

    key = os.environ.get("TRELLO_KEY", "")
    token = os.environ.get("TRELLO_TOKEN", "")
    board_id = os.environ.get("TRELLO_BOARD_SANDBOX_ID", "")
    list_id = os.environ.get("TRELLO_DEFAULT_LIST_ID", "")
    missing = [n for n, v in {
        "MISTRAL_API_KEY": (os.environ.get("MISTRAL_API_KEY", "")
                            or settings.mistral_api_key),
        "TRELLO_KEY": key, "TRELLO_TOKEN": token,
        "TRELLO_BOARD_SANDBOX_ID": board_id,
        "TRELLO_DEFAULT_LIST_ID": list_id}.items() if not v]
    if missing:
        print(__doc__)
        print(f"missing environment variables: {', '.join(missing)}")
        return 2

    db, trello, pipeline, gateway = build(board_id, list_id, key, token)
    try:
        pre_ids = {c["id"] for c in trello.board_cards()}
    except urllib.error.HTTPError as e:
        print(f"Trello error while reading the board: {e.code} {e.reason}")
        print(e.read().decode(errors="replace"))
        return 2

    try:
        if args.interactive:
            asyncio.run(run_interactive(pipeline, db))
            print(f"\n{len(gateway.replies)} assistant message(s) exchanged.")
            return 0
        message = args.message or SCENARIO_MSG
        fallback = None if args.message else SCENARIO_FALLBACK
        results = asyncio.run(run_scenario(pipeline, db, trello, gateway,
                                            pre_ids, message, fallback))
        ok = sum(results)
        print(f"\n{ok}/{len(results)} checks passed.")
        return 0 if ok == len(results) else 1
    except urllib.error.HTTPError as e:
        src = "Mistral" if "api.mistral.ai" in str(e.url) else "Trello"
        print(f"\n{src} API error: {e.code} {e.reason}")
        print(e.read().decode(errors="replace"))
        return 2
    finally:
        try:
            cleanup(trello, pre_ids, key, token, args.keep)
        except Exception as e:                      # never mask a test result
            print(f"cleanup failed ({e!r}) -- remove test cards manually.")


if __name__ == "__main__":
    sys.exit(main())

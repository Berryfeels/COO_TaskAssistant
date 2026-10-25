#!/usr/bin/env python3
"""Manual test against a real Trello SANDBOX board (build order phase 1).

Usage:
    export TRELLO_KEY=...
    export TRELLO_TOKEN=...
    export TRELLO_SANDBOX_LIST_ID=...      # id of a list (column) on the sandbox board
    export TRELLO_SANDBOX_BOARD_ID=...     # optional: scopes the search step to one board
    python3 scripts/trello_sandbox_test.py

It exercises: create -> mirror audit -> move date -> complete -> archive,
then deletes the test card. Nothing is written to your real boards:
use a dedicated sandbox board.

IMPORTANT: the list/board IDs needed here are the numeric Trello IDs, not
the names. Find them with:
    curl "https://api.trello.com/1/boards/<boardId>?key=..&token=.."
"""
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.schema import Action, Task
from app.trello.client import TrelloClient

PASS, FAIL = "\033[92mPASS\033[0m", "\033[91mFAIL\033[0m"


def check(name, cond, detail=""):
    print(f"  [{PASS if cond else FAIL}] {name}" + (f" -- {detail}" if not cond and detail else ""))
    return bool(cond)


def main() -> int:
    key = os.environ.get("TRELLO_KEY")
    token = os.environ.get("TRELLO_TOKEN")
    list_id = os.environ.get("TRELLO_SANDBOX_LIST_ID")
    # Optional but recommended: scope the search step to one board, so the
    # query never returns cards from other boards your token can access.
    board_id = os.environ.get("TRELLO_SANDBOX_BOARD_ID", "")
    if not all([key, token, list_id]):
        print(__doc__)
        return 2

    api = TrelloClient(key, token, board_id=board_id)
    tz = ZoneInfo("Europe/Berlin")
    results = []
    card_id = None

    try:
        print("1) create card")
        due = (datetime.now(tz) + timedelta(days=1)).isoformat()
        task = Task(action=Action.create, title="Sandbox test - call supplier",
                    description="mirror audit", due=due, list_id=list_id,
                    labels=[], assignees=[])
        card = api.create_card(task)
        card_id = card["id"]
        results.append(check("card created", bool(card_id)))

        print("2) mirror audit: Task -> card")
        results.append(check("title mirrored", card.get("name") == task.title,
                            f"{card.get('name')!r} != {task.title!r}"))
        results.append(check("desc mirrored", card.get("desc") == task.description))
        results.append(check("due mirrored", bool(card.get("due"))))
        results.append(check("idList mirrored", card.get("idList") == list_id))

        print("3) mirror audit: card -> Task (reverse mapping)")
        fetched = api.get_card(card_id)
        back = Task.from_card(fetched)
        results.append(check("round-trip title", back.title == task.title))
        results.append(check("round-trip list", back.list_id == list_id))
        diff = task.mirror_diff(fetched)
        # due is normalised by Trello (trailing Z / precision): ignore 'due'
        diff.pop("due", None)
        results.append(check("no unexpected diffs", not diff, str(diff)))

        print("4) move date")
        new_due = (datetime.now(tz) + timedelta(days=3)).isoformat()
        moved = api.update_card(Task(action=Action.move_date,
                                     target_card_id=card_id, due=new_due))
        results.append(check("due updated", bool(moved.get("due"))))

        print("5) complete")
        done = api.update_card(Task(action=Action.complete,
                                    target_card_id=card_id, done=True))
        results.append(check("dueComplete set", done.get("dueComplete") is True))

        print("6) card resolution (deterministic read)")
        bc = api.board_cards()
        results.append(check("board_cards sees the card immediately",
                             any(c["id"] == card_id for c in bc)))
        note = "search index lags for new cards (>40 s observed): expected miss"
        sres = api.search_cards("sandbox test")
        print(f"  [info] /1/search now: "
              f"{'found' if any(c['id'] == card_id for c in sres) else note}")

        print("7) archive (recoverable)")
        arch = api.update_card(Task(action=Action.archive,
                                    target_card_id=card_id, closed=True))
        results.append(check("closed set", arch.get("closed") is True))
        fetched2 = api.get_card(card_id)
        results.append(check("closed mirrored in from_card",
                             Task.from_card(fetched2).closed is True))

        print("8) search excludes archived card")
        found2 = api.search_cards("sandbox test")
        results.append(check("archived card not in search",
                             not any(c["id"] == card_id for c in found2)))

    finally:
        if card_id:
            print("9) cleanup: delete test card")
            url = (f"{api.BASE}/cards/{card_id}?"
                   + urllib.parse.urlencode({"key": key, "token": token}))
            req = urllib.request.Request(url, method="DELETE")
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
            print("  deleted.")

    ok = sum(results)
    print(f"\n{ok}/{len(results)} checks passed.")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

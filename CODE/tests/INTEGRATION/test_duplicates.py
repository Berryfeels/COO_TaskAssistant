"""Integration: the near-duplicate guard (spec 5.4) through the pipeline.
On every add_card the new title is compared against active pendings and all
open board cards (normalized sequence similarity >= 0.75). Hits are appended
to the outcome message as a heads-up - the write NEVER blocks."""
import unittest

from app.schema import Action, Card
from CODE.tests.SHARED.base import add, incoming, last_reply, make_pipeline


def board_card(cid, name, closed=False):
    return {"id": cid, "name": name, "idList": "Ltodo", "due": None,
            "dueComplete": False, "closed": closed}


class TestDuplicateGuard(unittest.TestCase):
    def test_reworded_duplicate_flags_existing_card_but_still_creates(self):
        h = make_pipeline(script=[add("call the supplier!!")])
        h.trello.cards = [board_card("c1", "Call the supplier")]
        incoming(h.pipe, "u", "m1", "add: call the supplier!!")
        self.assertEqual(len(h.trello.cards), 2)     # never blocks the write
        msg = last_reply(h.gateway)
        self.assertIn("Heads-up: similar card(s) found:", msg)
        self.assertIn("'Call the supplier' (already on the board)", msg)
        self.assertIn("duplicate", msg)

    def test_immediate_duplicate_is_created_and_flagged(self):
        h = make_pipeline(script=[add("Call supplier")])
        h.trello.cards = [board_card("c1", "Call supplier")]
        incoming(h.pipe, "u", "m1", "add: Call supplier")
        self.assertEqual(len(h.trello.cards), 2)
        self.assertIn("already on the board", last_reply(h.gateway))

    def test_duplicate_pending_is_flagged(self):
        h = make_pipeline(script=[add("prepare the cake")])
        pid = h.db.pending_save("u", add("Prepare cake"), 1000.0)
        incoming(h.pipe, "u", "m1", "add: prepare the cake")
        msg = last_reply(h.gateway)
        self.assertIn(f"'Prepare cake' (pending [{pid}])", msg)

    def test_unrelated_titles_not_flagged(self):
        h = make_pipeline(script=[add("Call the supplier")])
        h.trello.cards = [board_card("c1", "Send the invoice")]
        incoming(h.pipe, "u", "m1", "add: call the supplier")
        self.assertNotIn("Heads-up", last_reply(h.gateway))

    def test_closed_board_cards_are_ignored(self):
        h = make_pipeline(script=[add("Call supplier")])
        h.trello.cards = [board_card("c1", "Call supplier", closed=True)]
        incoming(h.pipe, "u", "m1", "add: Call supplier")
        self.assertNotIn("Heads-up", last_reply(h.gateway))

    def test_update_actions_are_not_flagged(self):
        # the guard is scoped to add_card only (spec 5.4)
        h = make_pipeline(script=[Card(action=Action.update,
                                       target_card_id="c1", title="Renamed")])
        h.trello.cards = [board_card("c1", "Call supplier")]
        incoming(h.pipe, "u", "m1", "rename c1")
        self.assertNotIn("Heads-up", last_reply(h.gateway))
        self.assertEqual(h.trello.cards[0]["name"], "Renamed")


if __name__ == "__main__":
    unittest.main()

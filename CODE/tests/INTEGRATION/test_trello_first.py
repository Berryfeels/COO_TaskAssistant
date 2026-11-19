"""Integration: Trello is the single source of truth - every write hits
Trello FIRST and the local DB only AFTER the write succeeded (spec 2.2/5.2).
Order is asserted with spies on the real DB and Trello fake."""
import unittest

from app.schema import Action, Card
from CODE.tests.SHARED.base import add, incoming, last_reply, make_pipeline


def spy_order(h):
    """Wrap trello.apply / db.mark_processed / db.list_save / pending.apply_by_id
    to record the exact write order."""
    events = []
    orig_apply = h.trello.apply

    def apply_spy(card):
        events.append(("trello_apply", card.action.value))
        return orig_apply(card)

    h.trello.apply = apply_spy
    orig_mark = h.db.mark_processed

    def mark_spy(mid):
        events.append(("db_mark_processed", mid))
        return orig_mark(mid)

    h.db.mark_processed = mark_spy
    orig_list_save = h.db.list_save

    def list_spy(lid, name, at):
        events.append(("db_list_save", lid))
        return orig_list_save(lid, name, at)

    h.db.list_save = list_spy
    orig_apply_by_id = h.pending.apply_by_id

    def pending_spy(pid):
        events.append(("db_pending_applied", pid))
        return orig_apply_by_id(pid)

    h.pending.apply_by_id = pending_spy
    return events


class TestWriteOrdering(unittest.TestCase):
    def test_immediate_write_trello_before_db(self):
        h = make_pipeline(script=[add("Call supplier")])
        events = spy_order(h)
        incoming(h.pipe, "u", "m1", "add: call supplier")
        self.assertEqual(events, [("trello_apply", "add_card"),
                                  ("db_mark_processed", "m1")])

    def test_add_list_mirrors_db_only_after_trello(self):
        h = make_pipeline(script=[Card(action=Action.add_list, title="Review")])
        events = spy_order(h)
        incoming(h.pipe, "u", "m1", "new column Review")
        self.assertEqual(events, [("trello_apply", "add_list"),
                                  ("db_list_save", h.trello.lists[-1]["id"]),
                                  ("db_mark_processed", "m1")])

    def test_confirmation_write_trello_before_db(self):
        h = make_pipeline()
        events = spy_order(h)
        pid = h.db.pending_save("u", add("old card"), 1000.0)
        incoming(h.pipe, "u", "m1", f"OK {pid}")
        self.assertEqual(events, [("trello_apply", "add_card"),
                                  ("db_pending_applied", pid),
                                  ("db_mark_processed", "m1")])


class TestArchive(unittest.TestCase):
    def test_archive_closes_the_card_in_trello_first(self):
        h = make_pipeline(script=[Card(action=Action.archive,
                                        target_card_id="c1", closed=True)])
        h.trello.cards = [{"id": "c1", "name": "Fusking task", "idList": "Ltodo",
                           "due": None, "dueComplete": False, "closed": False}]
        events = spy_order(h)
        incoming(h.pipe, "u", "m1", "Archive the fusking task")
        self.assertEqual(events, [("trello_apply", "archive"),
                                  ("db_mark_processed", "m1")])
        self.assertTrue(h.trello.cards[0]["closed"])      # PUT closed=true
        self.assertEqual(h.trello.cards[0]["name"], "Fusking task")  # not deleted
        self.assertIn("Done: 'Fusking task' archived in Trello.",
                      last_reply(h.gateway))


class TestFailedWriteLeavesDbConsistent(unittest.TestCase):
    def test_failed_write_notifies_and_nothing_is_recorded(self):
        h = make_pipeline(script=[add("X")])
        h.trello.fail_next(RuntimeError("HTTP 500"))
        incoming(h.pipe, "u", "m1", "add X")
        msg = last_reply(h.gateway)
        self.assertIn("could not write", msg)
        self.assertIn("NOT created", msg)
        self.assertEqual(h.trello.cards, [])              # nothing on the board
        # spec 8: the message is handled (marked processed), nothing written
        self.assertTrue(h.db.already_processed("m1"))

    def test_user_can_resend_after_failure(self):
        h = make_pipeline(script=[add("X"), add("X")])
        h.trello.fail_next(RuntimeError("HTTP 500"))
        incoming(h.pipe, "u", "m1", "add X")
        incoming(h.pipe, "u", "m2", "add X")              # new message id
        self.assertEqual(len(h.trello.cards), 1)
        self.assertIn("Done:", last_reply(h.gateway))

    def test_failed_confirmation_does_not_mark_pending_applied(self):
        h = make_pipeline()
        pid = h.db.pending_save("u", add("old"), 1000.0)
        h.trello.fail_next()
        incoming(h.pipe, "u", "m1", f"OK {pid}")
        self.assertIn("could not write", last_reply(h.gateway))
        self.assertIsNotNone(h.db.pending_get(pid))       # still pending
        self.assertEqual(h.trello.cards, [])

    def test_retry_after_failure_succeeds(self):
        h = make_pipeline()
        pid = h.db.pending_save("u", add("old"), 1000.0)
        h.trello.fail_next()
        incoming(h.pipe, "u", "m1", f"OK {pid}")
        incoming(h.pipe, "u", "m2", f"OK {pid}")          # retry works
        self.assertEqual(len(h.trello.cards), 1)
        status = h.db.conn.execute(
            "SELECT status FROM pending WHERE pending_id=?", (pid,)).fetchone()
        self.assertEqual(status["status"], "applied")


class TestQueryReadsTrello(unittest.TestCase):
    def test_query_shows_column_and_done_flag_as_on_the_board(self):
        h = make_pipeline(script=[Card(action=Action.query)])
        h.trello.cards = [
            {"id": "c1", "name": "Call supplier", "idList": "Ltodo",
             "due": "2026-11-17T08:00:00+01:00", "dueComplete": False,
             "closed": False},
            {"id": "c2", "name": "Ship invoice", "idList": "Ldone",
             "due": None, "dueComplete": True, "closed": False},
            {"id": "c3", "name": "Archived thing", "idList": "Ltodo",
             "due": None, "dueComplete": False, "closed": True},
        ]
        incoming(h.pipe, "u", "m1", "what are my cards?")
        msg = last_reply(h.gateway)
        self.assertTrue(msg.startswith("Your open cards (from Trello):"))
        self.assertIn("- Call supplier [To-do], due 2026-11-17T08:00:00+01:00", msg)
        self.assertIn("- Ship invoice [Done] (done)", msg)
        self.assertNotIn("Archived thing", msg)           # closed cards hidden
        # queries read Trello (board_cards + list_lists), never write
        self.assertIn(("board_cards",), h.trello.calls)
        self.assertIn(("list_lists",), h.trello.calls)
        self.assertNotIn("apply", [c[0] for c in h.trello.calls])
        self.assertTrue(h.db.already_processed("m1"))


if __name__ == "__main__":
    unittest.main()

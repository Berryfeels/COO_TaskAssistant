"""Integration: query actions are read-only and answered from Trello only
(spec 2.2/2.3) - never from the local DB."""
import unittest

from app.schema import Action, Card
from CODE.tests.SHARED.base import incoming, last_reply, make_pipeline


class TestQueries(unittest.TestCase):
    def test_list_my_cards_answers(self):
        h = make_pipeline(script=[Card(action=Action.query)])
        h.trello.cards = [
            {"id": "c1", "name": "Call supplier", "idList": "Ltodo", "due": None,
             "dueComplete": False, "closed": False},
            {"id": "c2", "name": "Prepare demo", "idList": "Ldoing", "due": None,
             "dueComplete": False, "closed": False},
        ]
        incoming(h.pipe, "u", "m1", "list my cards")
        msg = last_reply(h.gateway)
        self.assertIn("- Call supplier [To-do]", msg)
        self.assertIn("- Prepare demo [Doing]", msg)
        self.assertTrue(h.db.already_processed("m1"))

    def test_empty_board_answers_no_open_cards(self):
        h = make_pipeline(script=[Card(action=Action.query)])
        incoming(h.pipe, "u", "m1", "any cards?")
        self.assertEqual(last_reply(h.gateway), "You have no open cards.")
        self.assertTrue(h.db.already_processed("m1"))

    def test_unknown_column_renders_as_question_mark(self):
        h = make_pipeline(script=[Card(action=Action.query)])
        h.trello.cards = [{"id": "c1", "name": "Orphan", "idList": "Lgone",
                           "due": None, "dueComplete": False, "closed": False}]
        incoming(h.pipe, "u", "m1", "my cards")
        self.assertIn("- Orphan [?]", last_reply(h.gateway))

    def test_llm_failure_gets_a_reply_not_silence(self):
        h = make_pipeline(script=[ConnectionError("network down")])
        incoming(h.pipe, "u", "m1", "my cards")
        self.assertIn("something went wrong", last_reply(h.gateway))
        self.assertEqual(h.trello.calls, [])


if __name__ == "__main__":
    unittest.main()

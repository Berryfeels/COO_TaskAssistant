"""Integration: invalid write actions are rejected BEFORE any Trello call and
reported to the user (never applied as a silent no-op); Trello/LLM failures
always produce a reply - never silence (spec 8, error handling)."""
import unittest

from app.schema import Action, Card, Clarification
from CODE.tests.SHARED.base import add, incoming, last_reply, make_pipeline


class TestInvalidWriteReported(unittest.TestCase):
    def _invalid(self, card, *expect):
        h = make_pipeline(script=[card])
        incoming(h.pipe, "u", "m1", "whatever the user said")
        msg = last_reply(h.gateway)
        for e in expect:
            self.assertIn(e, msg)
        self.assertEqual(h.trello.calls, [])          # validate before write
        self.assertTrue(h.db.already_processed("m1"))

    def test_move_date_without_due_is_rejected_and_reported(self):
        self._invalid(Card(action=Action.move_date, target_card_id="c1"),
                      "could not build a valid move_date",
                      "move_date requires the new due date")

    def test_update_without_target_is_rejected_not_silent(self):
        self._invalid(Card(action=Action.update, title="x"),
                      "requires target_card_id")

    def test_update_without_change_is_rejected_and_reported(self):
        self._invalid(Card(action=Action.update, target_card_id="c1"),
                      "update requires a field to change")

    def test_add_card_without_title_is_rejected(self):
        self._invalid(Card(action=Action.add_card), "add_card requires a title")

    def test_update_with_done_flag_changes_the_card(self):
        h = make_pipeline(script=[Card(action=Action.update,
                                       target_card_id="c1", done=True)])
        h.trello.cards = [{"id": "c1", "name": "X", "idList": "Ltodo",
                           "dueComplete": False, "closed": False}]
        incoming(h.pipe, "u", "m1", "mark X done")
        self.assertTrue(h.trello.cards[0]["dueComplete"])
        self.assertIn("Done: 'X' updated in Trello.", last_reply(h.gateway))


class TestWriteErrorReported(unittest.TestCase):
    def test_failed_write_notifies_user_and_pipeline_survives(self):
        h = make_pipeline(script=[add("A"), add("B")])
        h.trello.fail_with = RuntimeError("boom")
        incoming(h.pipe, "u", "m1", "add A")
        self.assertIn("could not write", last_reply(h.gateway))
        h.trello.fail_with = None                    # Trello recovers
        incoming(h.pipe, "u", "m2", "add B")
        self.assertEqual(len(h.trello.cards), 1)
        self.assertIn("Done:", last_reply(h.gateway))


class TestLLMFailure(unittest.TestCase):
    def test_llm_failure_gets_a_reply_not_silence(self):
        h = make_pipeline(script=[RuntimeError("API down")])
        incoming(h.pipe, "u", "m1", "hello")
        msg = last_reply(h.gateway)
        self.assertIn("API down", msg)              # the problem is stated first
        self.assertIn("Nothing was written", msg)
        self.assertEqual(h.trello.calls, [])
        self.assertTrue(h.db.already_processed("m1"))

    def test_clarification_question_is_forwarded_verbatim(self):
        h = make_pipeline(script=[Clarification("Which card do you mean?")])
        incoming(h.pipe, "u", "m1", "move it to Friday")
        self.assertEqual(last_reply(h.gateway), "Which card do you mean?")
        self.assertEqual(h.trello.calls, [])
        self.assertTrue(h.db.already_processed("m1"))


class TestDefaultListFallback(unittest.TestCase):
    def test_idlist_falls_back_and_does_not_override(self):
        h = make_pipeline(script=[add("A"),
                                  Card(action=Action.add_card, title="B",
                                       list_id="Ldoing")])
        incoming(h.pipe, "u", "m1", "add A")
        incoming(h.pipe, "u", "m2", "add B to Doing")
        self.assertEqual(h.trello.cards[0]["idList"], "Ltodo")   # default
        self.assertEqual(h.trello.cards[1]["idList"], "Ldoing")  # explicit wins


if __name__ == "__main__":
    unittest.main()

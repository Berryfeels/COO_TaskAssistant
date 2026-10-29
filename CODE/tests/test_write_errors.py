import unittest

from tests.base import BaseCase

from app.schema import Action, Task
from app.trello.client import FakeTrello, TrelloClient


class FailingTrello(FakeTrello):
    """FakeTrello whose apply always fails, to test error reporting."""

    def apply(self, task):
        raise RuntimeError("HTTP 400 Bad Request")


class StubLLM:
    """Returns a fixed Task, to test what the pipeline does with it."""

    def __init__(self, task):
        self.task = task

    def interpret(self, text, user, trello):
        return self.task


class TestWriteErrorReported(BaseCase):
    def test_failed_write_notifies_user_and_survives(self):
        self.pipe.trello = FailingTrello()
        self.say("m1", "create task 'Call the supplier' for marco due tomorrow")
        msg = self.last_message()
        self.assertIn("could not write", msg)
        self.assertIn("Call the supplier", msg)
        self.assertIn("NOT created", msg)
        # the pipeline is still alive after the failure
        self.pipe.trello = FakeTrello()
        self.say("m3", "create task 'Another thing' for marco due tomorrow")
        self.assertIn("created in Trello", self.last_message())


class TestInvalidWriteReported(BaseCase):
    """A modification with no real change (e.g. move_date with no new
    date extracted) must never reach the confirmation loop: Trello would
    accept the PUT as a no-op and the bot would report it as done."""

    def test_move_date_without_due_is_rejected_and_reported(self):
        self.pipe.llm = StubLLM(Task(action=Action.move_date,
                                      target_card_id="card1"))
        self.say("m1", "move 'Go with the flow' to Friday")
        self.assertIn("could not build a valid move_date", self.last_message())
        self.assertEqual(self.pending.active(self.user), [])
        # the message was handled: a retry is a new process, not a replay
        self.assertTrue(self.db.already_processed("m1"))

    def test_update_without_change_is_rejected_and_reported(self):
        self.pipe.llm = StubLLM(Task(action=Action.update,
                                     target_card_id="card1"))
        self.say("m1", "update 'Go with the flow'")
        self.assertIn("could not build a valid update", self.last_message())
        self.assertEqual(self.pending.active(self.user), [])

    def test_update_without_target_is_rejected_not_silent(self):
        self.pipe.llm = StubLLM(Task(action=Action.update, title="x"))
        self.say("m1", "update something")
        self.assertIn("requires target_card_id", self.last_message())
        self.assertEqual(self.pending.active(self.user), [])

    def test_update_with_done_flag_changes_the_card(self):
        self.trello.cards["card1"] = {
            "id": "card1", "name": "Old", "desc": "", "due": None,
            "idMembers": [], "idLabels": [], "idList": "L",
            "closed": False, "dueComplete": False}
        self.pipe.llm = StubLLM(Task(action=Action.update,
                                      target_card_id="card1", done=True))
        self.say("m1", "mark 'Old' as done")
        self.assertTrue(self.trello.cards["card1"]["dueComplete"])


class TestDefaultListFallback(unittest.TestCase):
    """Creates with no list_id fall back to the configured default list."""

    def test_idlist_falls_back_and_does_not_override(self):
        captured = {}

        class Capture(TrelloClient):
            def _req(self, method, path, params):
                captured["args"] = (method, path, params)
                return {"id": "c1"}

        client = Capture("k", "t", board_id="b",
                         default_list_id="list-default")

        client.create_card(Task(action=Action.create, title="A"))
        self.assertEqual(captured["args"][2]["idList"], "list-default")

        client.create_card(Task(action=Action.create, title="B",
                                list_id="list-explicit"))
        self.assertEqual(captured["args"][2]["idList"], "list-explicit")

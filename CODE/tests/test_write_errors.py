import unittest

from tests.base import BaseCase

from app.schema import Action, Task
from app.trello.client import FakeTrello, TrelloClient


class FailingTrello(FakeTrello):
    """FakeTrello whose apply always fails, to test error reporting."""

    def apply(self, task):
        raise RuntimeError("HTTP 400 Bad Request")


class TestWriteErrorReported(BaseCase):
    def test_failed_write_notifies_user_and_survives(self):
        self.pipe.trello = FailingTrello()
        self.say("m1", "create task 'Call the supplier' for marco due tomorrow")
        pid = self.pending.active(self.user)[0].pending_id
        self.say("m2", f"OK {pid}")
        msg = self.last_message()
        self.assertIn("could not write", msg)
        self.assertIn("Call the supplier", msg)
        self.assertIn("NOT created", msg)
        # the pipeline is still alive after the failure
        self.pipe.trello = FakeTrello()
        self.say("m3", "create task 'Another thing' for marco due tomorrow")
        self.assertIn("OK ", self.last_message())


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

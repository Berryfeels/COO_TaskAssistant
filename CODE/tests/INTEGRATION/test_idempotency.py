"""Integration: idempotency (spec 5.2) - the same message must never be
processed twice; mark_processed means "handled", never "executed"."""
import unittest

from CODE.tests.SHARED.base import add, chat, incoming, last_reply, make_pipeline


class TestIdempotency(unittest.TestCase):
    def test_same_message_never_processed_twice(self):
        h = make_pipeline(script=[add("X")])
        incoming(h.pipe, "u", "m1", "add X")
        incoming(h.pipe, "u", "m1", "add X")     # redelivery, same message id
        self.assertEqual(len(h.llm.calls), 1)
        self.assertEqual(len(h.trello.cards), 1)
        self.assertEqual(len(h.gateway.sent), 1)

    def test_write_is_executed_immediately(self):
        h = make_pipeline(script=[add("X")])
        incoming(h.pipe, "u", "m1", "add X")
        self.assertEqual(len(h.trello.cards), 1)     # no pending, no waiting

    def test_mark_processed_is_not_confirmation(self):
        h = make_pipeline(script=[add("X")])
        incoming(h.pipe, "u", "m1", "add X")
        self.assertTrue(h.db.already_processed("m1"))
        self.assertEqual(h.pending.active("u"), [])  # nothing awaits "OK"
        self.assertNotIn("confirm", last_reply(h.gateway))

    def test_redelivered_confirmation_is_ignored(self):
        h = make_pipeline()
        pid = h.db.pending_save("u", add("old card"), 1000.0)
        incoming(h.pipe, "u", "m1", f"OK {pid}")
        incoming(h.pipe, "u", "m1", f"OK {pid}")     # same message id again
        applies = [c for c in h.trello.calls if c[0] == "apply"]
        self.assertEqual(len(applies), 1)
        self.assertEqual(len(h.llm.calls), 0)        # never reached the LLM

    def test_resent_confirmation_with_new_id_does_not_rewrite(self):
        # the user re-sends "OK <id>" as a NEW message: the pending is already
        # applied, so it must not be written to Trello a second time
        h = make_pipeline(script=[chat("already done")])
        pid = h.db.pending_save("u", add("old card"), 1000.0)
        incoming(h.pipe, "u", "m1", f"OK {pid}")
        incoming(h.pipe, "u", "m2", f"OK {pid}")
        applies = [c for c in h.trello.calls if c[0] == "apply"]
        self.assertEqual(len(applies), 1)
        self.assertEqual(len(h.trello.cards), 1)


if __name__ == "__main__":
    unittest.main()

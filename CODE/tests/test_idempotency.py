from tests.base import BaseCase


class TestIdempotency(BaseCase):
    def test_same_message_never_processed_twice(self):
        self.say("m1", "create task 'Call supplier' for marco due tomorrow")
        self.assertEqual(len(self.sent), 1)          # one confirmation
        # replay the exact same message id
        self.say("m1", "create task 'Call supplier' for marco due tomorrow")
        self.assertEqual(len(self.sent), 1)          # no duplicate
        self.assertEqual(len(self.trello.cards), 1)   # written exactly once

    def test_write_is_executed_immediately(self):
        self.say("m1", "create task 'Call supplier' for marco")
        self.assertTrue(self.db.already_processed("m1"))
        self.assertEqual(len(self.trello.cards), 1)   # executed, not pending
        self.assertEqual(self.pending.active(self.user), [])

from tests.base import BaseCase


class TestIdempotency(BaseCase):
    def test_same_message_never_processed_twice(self):
        self.say("m1", "create task 'Call supplier' for marco due tomorrow")
        self.assertEqual(len(self.sent), 1)          # one confirmation
        # replay the exact same message id
        self.say("m1", "create task 'Call supplier' for marco due tomorrow")
        self.assertEqual(len(self.sent), 1)          # no duplicate, no second pending

    def test_mark_processed_is_not_confirmation(self):
        self.say("m1", "create task 'Call supplier' for marco")
        self.assertTrue(self.db.already_processed("m1"))
        active = self.pending.active(self.user)
        self.assertEqual(len(active), 1)             # still pending, NOT executed
        self.assertEqual(len(self.trello.cards), 0)  # no Trello write yet

    def test_confirmation_message_also_processed_once(self):
        self.say("m1", "create task 'Call supplier' for marco")
        pid = self.pending.active(self.user)[0].pending_id
        self.say("m2", f"ok {pid}")
        self.assertEqual(len(self.trello.cards), 1)
        # duplicate confirmation ignored
        self.say("m2", f"ok {pid}")
        self.assertEqual(len(self.trello.cards), 1)

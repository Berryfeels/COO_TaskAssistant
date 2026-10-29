from tests.base import BaseCase


class TestFIFO(BaseCase):
    def test_each_message_is_an_independent_process(self):
        """Message 2 must not be blocked by message 1 (spec 5.3)."""
        self.say("m1", "create task 'Call supplier' for marco")
        self.say("m2", "create task 'Send invoice' for anna")
        # both applied immediately, in arrival order
        self.assertEqual([c["name"] for c in self.trello.cards.values()],
                         ["Call supplier", "Send invoice"])

    def test_write_applied_without_confirmation(self):
        """No confirmation loop: the write lands and the bot only confirms."""
        self.say("m1", "create task 'Call supplier' for marco")
        self.assertIn("created in Trello", self.last_message())
        self.assertEqual(len(self.trello.cards), 1)
        self.assertEqual(self.pending.active(self.user), [])

    def test_stray_ok_does_not_rewrite_anything(self):
        self.say("m1", "create task 'Call supplier' for marco")
        self.say("m2", "ok")          # not a confirmation of anything
        self.assertEqual(len(self.trello.cards), 1)

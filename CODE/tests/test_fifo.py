from tests.base import BaseCase


class TestFIFO(BaseCase):
    def test_independent_pending_processes(self):
        """Message 2 must not be blocked by unconfirmed message 1 (spec 5.3)."""
        self.say("m1", "create task 'Call supplier' for marco")
        self.say("m2", "create task 'Send invoice' for anna")
        # both pending, neither executed
        active = self.pending.active(self.user)
        self.assertEqual(len(active), 2)
        self.assertEqual(len(self.trello.cards), 0)

    def test_each_confirmation_bound_to_its_pending_id(self):
        self.say("m1", "create task 'Call supplier' for marco")
        self.say("m2", "create task 'Send invoice' for anna")
        p1, p2 = self.pending.active(self.user)
        # confirm only the second one
        self.say("m3", f"ok {p2.pending_id}")
        self.assertEqual(len(self.trello.cards), 1)
        self.assertEqual(self.trello.cards["card1"]["name"], "Send invoice")
        # the first is still pending
        self.assertEqual(len(self.pending.active(self.user)), 1)

    def test_bare_ok_with_multiple_pending_is_ambiguous(self):
        self.say("m1", "create task 'Call supplier' for marco")
        self.say("m2", "create task 'Send invoice' for anna")
        self.say("m3", "ok")
        self.assertIn("several pending", self.last_message().lower())
        self.assertEqual(len(self.trello.cards), 0)

    def test_bare_ok_with_single_pending_applies_it(self):
        self.say("m1", "create task 'Call supplier' for marco")
        self.say("m2", "ok")
        self.assertEqual(len(self.trello.cards), 1)

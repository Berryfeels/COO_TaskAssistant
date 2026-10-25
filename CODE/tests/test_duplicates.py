from tests.base import BaseCase


class TestDuplicateGuard(BaseCase):
    """Near-duplicate guard: message-ID idempotency (spec 5.2) cannot catch
    a reworded repeat of a forgotten task - the pipeline must warn."""

    def test_reworded_duplicate_flags_existing_card(self):
        self.say("m1", "create task 'Call the supplier' for marco due tomorrow")
        pid = self.pending.active(self.user)[0].pending_id
        self.say("m2", f"OK {pid}")
        self.assertEqual(len(self.trello.cards), 1)
        # the user forgot and prompts again, wording and punctuation differ
        self.say("m3", "create task 'call the supplier!!' for marco")
        msg = self.last_message()
        self.assertIn("similar", msg)
        self.assertIn("already on the board", msg)

    def test_duplicate_pending_flagged_before_any_write(self):
        self.say("m1", "create task 'Call the supplier' for marco due tomorrow")
        self.say("m2", "create task 'Call the supplier' for marco due tomorrow")
        self.assertEqual(len(self.pending.active(self.user)), 2)
        self.assertIn("pending", self.last_message())
        # nothing written yet: both wait for their own confirmation
        self.assertEqual(len(self.trello.cards), 0)

    def test_unrelated_titles_not_flagged(self):
        self.say("m1", "create task 'Call the supplier' for marco due tomorrow")
        self.say("m2", "create task 'Order new laptops' for anna due tomorrow")
        self.assertNotIn("similar", self.last_message())

    def test_confirmed_cards_are_scanned_board_wide(self):
        self.say("m1", "create task 'Call the supplier' for marco due tomorrow")
        pid = self.pending.active(self.user)[0].pending_id
        self.say("m2", f"OK {pid}")
        # once confirmed, the card itself is scanned: a repeat is flagged
        self.say("m3", "create task 'Call the supplier' for anna due tomorrow")
        self.assertIn("already on the board", self.last_message())

    def test_update_actions_are_not_flagged(self):
        self.say("m1", "create task 'Call the supplier' for marco due tomorrow")
        pid = self.pending.active(self.user)[0].pending_id
        self.say("m2", f"OK {pid}")
        self.say("m3", "complete 'Call the supplier'")
        self.assertNotIn("similar", self.last_message())

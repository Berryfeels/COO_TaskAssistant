from tests.base import BaseCase


class TestDuplicateGuard(BaseCase):
    """Near-duplicate guard: message-ID idempotency (spec 5.2) cannot catch
    a reworded repeat of a forgotten task - the pipeline must warn.

    Since the confirmation loop was removed the heads-up arrives AFTER the
    write: it is informational, the card is created regardless."""

    def test_reworded_duplicate_flags_existing_card(self):
        self.say("m1", "create task 'Call the supplier' for marco due tomorrow")
        self.assertEqual(len(self.trello.cards), 1)
        # the user forgot and prompts again, wording and punctuation differ
        self.say("m3", "create task 'call the supplier!!' for marco")
        msg = self.last_message()
        self.assertIn("similar", msg)
        self.assertIn("already on the board", msg)
        # never blocked: the second card is created too
        self.assertEqual(len(self.trello.cards), 2)

    def test_immediate_duplicate_is_created_and_flagged(self):
        self.say("m1", "create task 'Call the supplier' for marco due tomorrow")
        self.say("m2", "create task 'Call the supplier' for marco due tomorrow")
        self.assertEqual(len(self.trello.cards), 2)
        self.assertIn("already on the board", self.last_message())

    def test_unrelated_titles_not_flagged(self):
        self.say("m1", "create task 'Call the supplier' for marco due tomorrow")
        self.say("m2", "create task 'Order new laptops' for anna due tomorrow")
        self.assertNotIn("similar", self.last_message())

    def test_cards_are_scanned_board_wide(self):
        # once written, the card itself is scanned: a repeat is flagged
        self.say("m1", "create task 'Call the supplier' for marco due tomorrow")
        self.say("m3", "create task 'Call the supplier' for anna due tomorrow")
        self.assertIn("already on the board", self.last_message())

    def test_update_actions_are_not_flagged(self):
        self.say("m1", "create task 'Call the supplier' for marco due tomorrow")
        self.say("m3", "complete 'Call the supplier'")
        self.assertNotIn("similar", self.last_message())

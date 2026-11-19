"""Unit tests for the notification builder: every render_* message.

The pipeline tests only assert fragments; these pin down the exact user
-facing text of confirmations, reminders, cancellation and error notices.
"""
import unittest
from types import SimpleNamespace

from app.notify import builder
from app.schema import Action, Card


def card(**kw) -> Card:
    defaults = dict(action=Action.add_card, title="Call supplier")
    defaults.update(kw)
    return Card(**defaults)


def pending_row(pending_id: str, t: Card) -> SimpleNamespace:
    return SimpleNamespace(pending_id=pending_id, card=t)


class TestRenderConfirmation(unittest.TestCase):
    def test_create_with_everything(self):
        msg = builder.render_confirmation(
            "p1", card(assignees=["m1", "m2"], due="2026-11-01T10:00:00+01:00"))
        self.assertEqual(
            msg, "Add card 'Call supplier' for m1, m2, "
                 "due 2026-11-01T10:00:00+01:00. "
                 'Reply "OK p1" to confirm. [p1]')

    def test_create_minimal_has_no_due_or_assignee(self):
        msg = builder.render_confirmation("p2", card())
        self.assertEqual(msg, "Add card 'Call supplier'. "
                              'Reply "OK p2" to confirm. [p2]')
        self.assertNotIn("due", msg)

    def test_non_create_uses_title_or_target(self):
        msg = builder.render_confirmation(
            "p3", card(action=Action.complete, title="", target_card_id="card9"))
        self.assertTrue(msg.startswith("Mark as done 'card9'"))
        self.assertIn("OK p3", msg)

    def test_similar_cards_appended_as_heads_up(self):
        msg = builder.render_confirmation(
            "p4", card(), similar=["Call the supplier (card1)"])
        self.assertIn("Heads-up: similar card(s) found: "
                      "Call the supplier (card1).", msg)
        self.assertIn("different card", msg)

    def test_similar_none_is_not_appended(self):
        msg = builder.render_confirmation("p5", card(), similar=None)
        self.assertNotIn("Heads-up", msg)


class TestRenderOutcome(unittest.TestCase):
    def test_result_prefers_card_name(self):
        msg = builder.render_result(card(), {"name": "Renamed on board"})
        self.assertIn("'Renamed on board'", msg)

    def test_result_says_created_for_create(self):
        msg = builder.render_result(card(), {"name": "Call supplier"})
        self.assertEqual(msg, "Done: 'Call supplier' created in Trello.")

    def test_result_says_updated_for_other_actions(self):
        msg = builder.render_result(
            card(action=Action.move_date, title="", target_card_id="c1",
                 due="2026-11-01T10:00:00+01:00"), {"name": "Call supplier"})
        self.assertEqual(msg, "Done: 'Call supplier' updated in Trello.")

    def test_result_says_archived_for_archive(self):
        msg = builder.render_result(
            card(action=Action.archive, title="", target_card_id="c1",
                 closed=True), {"name": "Fusking task"})
        self.assertEqual(msg, "Done: 'Fusking task' archived in Trello.")

    def test_result_similar_appended_as_heads_up(self):
        msg = builder.render_result(
            card(), {"name": "Call supplier"},
            similar=["'Call the supplier' (already on the board)"])
        self.assertIn("Heads-up: similar card(s) found:", msg)
        self.assertIn("already on the board", msg)

    def test_result_without_similar_has_no_heads_up(self):
        msg = builder.render_result(card(), {"name": "Call supplier"})
        self.assertNotIn("Heads-up", msg)

    def test_write_error_says_not_created(self):
        msg = builder.render_write_error(card(), RuntimeError("HTTP 400"))
        self.assertIn("could not write", msg)
        self.assertIn("HTTP 400", msg)
        self.assertIn("NOT created", msg)

    def test_reminder_names_hours_and_id(self):
        msg = builder.render_reminder("p6", card(), hours_left=7)
        self.assertIn("[p6]", msg)
        self.assertIn("'Call supplier'", msg)
        self.assertIn("about 7 h", msg)
        self.assertIn("OK p6", msg)

    def test_cancelled_mentions_24h(self):
        msg = builder.render_cancelled("p7", card())
        self.assertIn("[p7]", msg)
        self.assertIn("24 hours", msg)
        self.assertIn("cancelled", msg)

    def test_clarification_passthrough(self):
        self.assertEqual(builder.render_clarification("Who?"), "Who?")


class TestRenderAmbiguous(unittest.TestCase):
    ROWS = [pending_row("p1", card(title="First")),
            pending_row("p2", card(action=Action.update, title="",
                                   target_card_id="card2"))]

    def test_lists_every_pending_with_ids(self):
        msg = builder.render_ambiguous(self.ROWS)
        self.assertIn("p1: add_card 'First'", msg)
        self.assertIn("p2: update 'card2'", msg)

    def test_suggests_first_pending_id(self):
        self.assertIn('Reply e.g. "OK p1"', builder.render_ambiguous(self.ROWS))

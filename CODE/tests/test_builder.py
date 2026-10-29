"""Unit tests for the notification builder: every render_* message.

The pipeline tests only assert fragments; these pin down the exact user
-facing text of confirmations, reminders, cancellation and error notices.
"""
import unittest
from types import SimpleNamespace

from app.notify import builder
from app.schema import Action, Task


def task(**kw) -> Task:
    defaults = dict(action=Action.create, title="Call supplier")
    defaults.update(kw)
    return Task(**defaults)


def pending_row(pending_id: str, t: Task) -> SimpleNamespace:
    return SimpleNamespace(pending_id=pending_id, task=t)


class TestRenderConfirmation(unittest.TestCase):
    def test_create_with_everything(self):
        msg = builder.render_confirmation(
            "p1", task(assignees=["m1", "m2"], due="2026-11-01T10:00:00+01:00"))
        self.assertEqual(
            msg, "Create task 'Call supplier' for m1, m2, "
                 "due 2026-11-01T10:00:00+01:00. "
                 'Reply "OK p1" to confirm. [p1]')

    def test_create_minimal_has_no_due_or_assignee(self):
        msg = builder.render_confirmation("p2", task())
        self.assertEqual(msg, "Create task 'Call supplier'. "
                              'Reply "OK p2" to confirm. [p2]')
        self.assertNotIn("due", msg)

    def test_non_create_uses_title_or_target(self):
        msg = builder.render_confirmation(
            "p3", task(action=Action.complete, title="", target_card_id="card9"))
        self.assertTrue(msg.startswith("Mark as done 'card9'"))
        self.assertIn("OK p3", msg)

    def test_similar_tasks_appended_as_heads_up(self):
        msg = builder.render_confirmation(
            "p4", task(), similar=["Call the supplier (card1)"])
        self.assertIn("Heads-up: similar task(s) found: "
                      "Call the supplier (card1).", msg)
        self.assertIn("different task", msg)

    def test_similar_none_is_not_appended(self):
        msg = builder.render_confirmation("p5", task(), similar=None)
        self.assertNotIn("Heads-up", msg)


class TestRenderOutcome(unittest.TestCase):
    def test_result_prefers_card_name(self):
        msg = builder.render_result(task(), {"name": "Renamed on board"})
        self.assertIn("'Renamed on board'", msg)

    def test_result_says_created_for_create(self):
        msg = builder.render_result(task(), {"name": "Call supplier"})
        self.assertEqual(msg, "Done: 'Call supplier' created in Trello.")

    def test_result_says_updated_for_other_actions(self):
        msg = builder.render_result(
            task(action=Action.move_date, title="", target_card_id="c1",
                 due="2026-11-01T10:00:00+01:00"), {"name": "Call supplier"})
        self.assertEqual(msg, "Done: 'Call supplier' updated in Trello.")

    def test_result_similar_appended_as_heads_up(self):
        msg = builder.render_result(
            task(), {"name": "Call supplier"},
            similar=["'Call the supplier' (already on the board)"])
        self.assertIn("Heads-up: similar task(s) found:", msg)
        self.assertIn("already on the board", msg)

    def test_result_without_similar_has_no_heads_up(self):
        msg = builder.render_result(task(), {"name": "Call supplier"})
        self.assertNotIn("Heads-up", msg)

    def test_write_error_says_not_created(self):
        msg = builder.render_write_error(task(), RuntimeError("HTTP 400"))
        self.assertIn("could not write", msg)
        self.assertIn("HTTP 400", msg)
        self.assertIn("NOT created", msg)

    def test_reminder_names_hours_and_id(self):
        msg = builder.render_reminder("p6", task(), hours_left=7)
        self.assertIn("[p6]", msg)
        self.assertIn("'Call supplier'", msg)
        self.assertIn("about 7 h", msg)
        self.assertIn("OK p6", msg)

    def test_cancelled_mentions_24h(self):
        msg = builder.render_cancelled("p7", task())
        self.assertIn("[p7]", msg)
        self.assertIn("24 hours", msg)
        self.assertIn("cancelled", msg)

    def test_clarification_passthrough(self):
        self.assertEqual(builder.render_clarification("Who?"), "Who?")


class TestRenderAmbiguous(unittest.TestCase):
    ROWS = [pending_row("p1", task(title="First")),
            pending_row("p2", task(action=Action.update, title="",
                                   target_card_id="card2"))]

    def test_lists_every_pending_with_ids(self):
        msg = builder.render_ambiguous(self.ROWS)
        self.assertIn("p1: create 'First'", msg)
        self.assertIn("p2: update 'card2'", msg)

    def test_suggests_first_pending_id(self):
        self.assertIn('Reply e.g. "OK p1"', builder.render_ambiguous(self.ROWS))

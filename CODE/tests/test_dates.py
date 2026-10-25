from datetime import datetime
from zoneinfo import ZoneInfo

import unittest

from app.dates import in_quiet_hours, resolve_relative
from app.schema import Action, Task
from tests.base import BaseCase

TZ = ZoneInfo("Europe/Berlin")


class TestDates(unittest.TestCase):
    def test_tomorrow(self):
        base = datetime(2026, 10, 25, 14, 0, tzinfo=TZ)  # a Sunday
        self.assertEqual(resolve_relative(base, "tomorrow").weekday(), 0)

    def test_next_tuesday_not_today(self):
        base = datetime(2026, 10, 27, 9, 0, tzinfo=TZ)   # a Tuesday
        nxt = resolve_relative(base, "next tuesday")
        self.assertEqual((nxt - base).days, 7)

    def test_bare_weekday_today_counts(self):
        base = datetime(2026, 10, 27, 9, 0, tzinfo=TZ)
        self.assertEqual((resolve_relative(base, "tuesday") - base).days, 0)

    def test_quiet_hours(self):
        self.assertTrue(in_quiet_hours(datetime(2026, 1, 1, 3, 0, tzinfo=TZ)))
        self.assertTrue(in_quiet_hours(datetime(2026, 1, 1, 2, 30, tzinfo=TZ)))
        self.assertFalse(in_quiet_hours(datetime(2026, 1, 1, 5, 0, tzinfo=TZ)))
        self.assertFalse(in_quiet_hours(datetime(2026, 1, 1, 14, 0, tzinfo=TZ)))


class TestSchema(BaseCase):
    def test_create_requires_title(self):
        with self.assertRaises(ValueError):
            Task(action=Action.create).validate()

    def test_update_requires_target(self):
        with self.assertRaises(ValueError):
            Task(action=Action.update, title="x").validate()

    def test_low_confidence_needs_clarification(self):
        t = Task(action=Action.create, title="x", confidence=0.3)
        self.assertTrue(t.needs_clarification)


class TestMirror(unittest.TestCase):
    """Task <-> Trello card mirror (spec section 4 + reverse mapping)."""

    CARD = {"id": "c1", "name": "Call supplier", "desc": "context",
            "idMembers": ["m1"], "due": "2026-10-30T10:00:00.000Z",
            "dueComplete": False, "closed": False,
            "idList": "l1", "idLabels": ["lab1"]}

    def test_from_card_reverse_mapping(self):
        t = Task.from_card(self.CARD)
        self.assertEqual(t.target_card_id, "c1")
        self.assertEqual(t.title, "Call supplier")
        self.assertEqual(t.description, "context")
        self.assertEqual(t.assignees, ["m1"])
        self.assertEqual(t.due, self.CARD["due"])
        self.assertEqual(t.list_id, "l1")
        self.assertEqual(t.labels, ["lab1"])
        self.assertFalse(t.closed)
        self.assertFalse(t.done)

    def test_mirror_diff_empty_when_identical(self):
        t = Task.from_card(self.CARD)
        t.description = "context"
        t.title = "Call supplier"
        self.assertEqual(t.mirror_diff(self.CARD), {})

    def test_mirror_diff_detects_changes(self):
        t = Task.from_card(self.CARD)
        t.title = "Call supplier X"
        diff = t.mirror_diff(self.CARD)
        self.assertEqual(diff["title"]["task"], "Call supplier X")
        self.assertEqual(diff["title"]["card"], "Call supplier")

    def test_mirror_ignores_none_task_fields(self):
        t = Task.from_card(self.CARD)
        t.list_id = None                      # unspecified on our side
        self.assertNotIn("list_id", t.mirror_diff(self.CARD))


if __name__ == "__main__":
    import sys, unittest
    sys.path.insert(0, ".")
    unittest.main()

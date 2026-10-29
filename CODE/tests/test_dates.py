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

    def test_move_date_requires_due(self):
        with self.assertRaises(ValueError):
            Task(action=Action.move_date, target_card_id="card1").validate()

    def test_update_requires_a_change(self):
        with self.assertRaises(ValueError):
            Task(action=Action.update, target_card_id="card1").validate()

    def test_update_with_change_passes(self):
        Task(action=Action.update, target_card_id="card1",
             due="2026-11-01T10:00:00+01:00").validate()
        Task(action=Action.update, target_card_id="card1",
             done=True).validate()

    def test_low_confidence_needs_clarification(self):
        t = Task(action=Action.create, title="x", confidence=0.3)
        self.assertTrue(t.needs_clarification)


if __name__ == "__main__":
    import sys, unittest
    sys.path.insert(0, ".")
    unittest.main()

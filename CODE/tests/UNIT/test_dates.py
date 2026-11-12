from datetime import datetime
from zoneinfo import ZoneInfo

import unittest

from app.dates import in_quiet_hours, resolve_relative
from app.schema import Action, Card, parse_action
from app.storage.db import DB

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


class TestSchema(unittest.TestCase):
    def test_create_requires_title(self):
        with self.assertRaises(ValueError):
            Card(action=Action.add_card).validate()

    def test_update_requires_target(self):
        with self.assertRaises(ValueError):
            Card(action=Action.update, title="x").validate()

    def test_move_date_requires_due(self):
        with self.assertRaises(ValueError):
            Card(action=Action.move_date, target_card_id="card1").validate()

    def test_update_requires_a_change(self):
        with self.assertRaises(ValueError):
            Card(action=Action.update, target_card_id="card1").validate()

    def test_update_with_change_passes(self):
        Card(action=Action.update, target_card_id="card1",
             due="2026-11-01T10:00:00+01:00").validate()
        Card(action=Action.update, target_card_id="card1",
             done=True).validate()

    def test_low_confidence_needs_clarification(self):
        t = Card(action=Action.add_card, title="x", confidence=0.3)
        self.assertTrue(t.needs_clarification)

    def test_add_list_without_title_defaults_to_new_list(self):
        for title in ("", None):
            t = Card(action=Action.add_list, title=title)
            t.validate()
            self.assertEqual(t.title, "new List")

    def test_parse_action_maps_legacy_create(self):
        self.assertIs(parse_action("create"), Action.add_card)
        self.assertIs(parse_action("add_card"), Action.add_card)
        self.assertIs(parse_action(Action.add_list), Action.add_list)
        with self.assertRaises(ValueError):
            parse_action("nonsense")

    def test_legacy_pending_row_loads_as_add_card(self):
        db = DB(":memory:")
        db.conn.execute(
            "INSERT INTO pending (pending_id, user_id, card_json, created_at, status) "
            "VALUES ('P1', 'u', ?, 0, 'pending')",
            ('{"action": "create", "title": "old card"}',))
        db.conn.commit()
        row = db.pending_active("u")[0]
        self.assertIs(row.card.action, Action.add_card)
        card = db.pending_set_status("P1", "applied")
        self.assertIs(card.action, Action.add_card)


class TestResolveRelativeEdges(unittest.TestCase):
    def test_today_returns_base_unchanged(self):
        base = datetime(2026, 10, 25, 14, 0, tzinfo=TZ)  # a Sunday
        self.assertEqual(resolve_relative(base, "today"), base)

    def test_next_weekday_from_weekend(self):
        base = datetime(2026, 10, 24, 9, 0, tzinfo=TZ)   # a Saturday
        self.assertEqual((resolve_relative(base, "next monday") - base).days, 2)

    def test_bare_weekday_already_passed_goes_to_next_week(self):
        base = datetime(2026, 10, 28, 9, 0, tzinfo=TZ)   # a Wednesday
        self.assertEqual((resolve_relative(base, "tuesday") - base).days, 6)

    def test_case_and_whitespace_insensitive(self):
        base = datetime(2026, 10, 27, 9, 0, tzinfo=TZ)   # a Tuesday
        self.assertEqual((resolve_relative(base, "  NEXT  Tuesday ") - base).days, 7)

    def test_unknown_reference_raises(self):
        with self.assertRaises(ValueError):
            resolve_relative(datetime(2026, 10, 25, tzinfo=TZ), "someday")

    def test_quiet_hours_boundaries(self):
        self.assertTrue(in_quiet_hours(datetime(2026, 1, 1, 2, 0, tzinfo=TZ)))
        self.assertFalse(in_quiet_hours(datetime(2026, 1, 1, 5, 0, tzinfo=TZ)))
        self.assertFalse(in_quiet_hours(datetime(2026, 1, 1, 1, 59, tzinfo=TZ)))


if __name__ == "__main__":
    import sys, unittest
    sys.path.insert(0, ".")
    unittest.main()

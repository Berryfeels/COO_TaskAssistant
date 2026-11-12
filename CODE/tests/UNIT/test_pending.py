"""Unit tests for app/confirm/pending.py: confirmation matching (spec 5.3),
reminder scheduling inputs and expiry (spec 5.1.3-5.1.4)."""
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import Settings
from app.confirm.pending import PendingService
from app.schema import Action, Card
from app.storage.db import DB

TZ = ZoneInfo("Europe/Berlin")


def make():
    db = DB(":memory:")
    return PendingService(db, Settings()), db


def add(title="Call supplier") -> Card:
    return Card(action=Action.add_card, title=title)


class TestMatchConfirmation(unittest.TestCase):
    def setUp(self):
        self.svc, self.db = make()

    def test_no_pendings_ok_is_not_a_confirmation(self):
        d = self.svc.match_confirmation("u", "OK")
        self.assertEqual(d.kind, "not_confirmation")

    def test_ok_with_pending_id_confirms_that_action(self):
        pid = self.db.pending_save("u", add("card A"), 1000.0)
        self.db.pending_save("u", add("card B"), 2000.0)
        d = self.svc.match_confirmation("u", f"OK {pid}")
        self.assertEqual(d.kind, "confirm")
        self.assertEqual(d.pending_id, pid)
        self.assertEqual(d.card.title, "card A")

    def test_pending_id_without_affirmative_is_a_new_instruction(self):
        pid = self.db.pending_save("u", add(), 1000.0)
        d = self.svc.match_confirmation("u", pid)
        self.assertEqual(d.kind, "not_confirmation")

    def test_bare_affirmative_with_single_pending_confirms(self):
        pid = self.db.pending_save("u", add(), 1000.0)
        for word in ("OK", "ok", "yes", "YES", "confirm"):
            d = self.svc.match_confirmation("u", word)
            self.assertEqual(d.kind, "confirm", word)
            self.assertEqual(d.pending_id, pid)

    def test_bare_affirmative_with_several_pendings_is_ambiguous(self):
        p1 = self.db.pending_save("u", add("A"), 1000.0)
        p2 = self.db.pending_save("u", add("B"), 2000.0)
        d = self.svc.match_confirmation("u", "OK")
        self.assertEqual(d.kind, "ambiguous")
        self.assertEqual({r.pending_id for r in d.active}, {p1, p2})

    def test_unrelated_text_is_not_a_confirmation(self):
        self.db.pending_save("u", add(), 1000.0)
        d = self.svc.match_confirmation("u", "add another card please")
        self.assertEqual(d.kind, "not_confirmation")

    def test_applied_pending_no_longer_matches(self):
        pid = self.db.pending_save("u", add(), 1000.0)
        self.svc.apply_by_id(pid)
        d = self.svc.match_confirmation("u", f"OK {pid}")
        self.assertEqual(d.kind, "not_confirmation")

    def test_matching_is_read_only(self):
        # matching must never change the status: the DB may say "applied"
        # only AFTER the Trello write succeeded (the pipeline does that)
        pid = self.db.pending_save("u", add(), 1000.0)
        self.svc.match_confirmation("u", f"OK {pid}")
        self.assertIsNotNone(self.db.pending_get(pid))   # still pending
        self.assertEqual(
            self.db.conn.execute(
                "SELECT status FROM pending WHERE pending_id=?",
                (pid,)).fetchone()["status"], "pending")


class TestRemindersAndQuietHours(unittest.TestCase):
    def setUp(self):
        self.svc, self.db = make()

    def test_due_for_reminder_uses_settings_interval_and_max(self):
        now = 1_000_000.0
        self.db.pending_save("u", add(), now)
        self.assertEqual(len(self.svc.due_for_reminder(now)), 1)
        pid = self.db.pending_active("u")[0].pending_id
        self.svc.bump_reminder(pid, now)
        self.assertEqual(len(self.svc.due_for_reminder(now + 6 * 3600)), 0)
        self.assertEqual(len(self.svc.due_for_reminder(now + 7 * 3600)), 1)

    def test_quiet_hours_window_2_to_5_berlin(self):
        self.assertTrue(self.svc.quiet_hours(datetime(2026, 1, 1, 2, 0, tzinfo=TZ)))
        self.assertTrue(self.svc.quiet_hours(datetime(2026, 1, 1, 4, 59, tzinfo=TZ)))
        self.assertFalse(self.svc.quiet_hours(datetime(2026, 1, 1, 5, 0, tzinfo=TZ)))
        self.assertFalse(self.svc.quiet_hours(datetime(2026, 1, 1, 12, 0, tzinfo=TZ)))
        self.assertFalse(self.svc.quiet_hours(datetime(2026, 1, 1, 1, 59, tzinfo=TZ)))


class TestExpiry(unittest.TestCase):
    def setUp(self):
        self.svc, self.db = make()

    def test_expire_cancels_and_returns_rows(self):
        now = 2_000_000.0
        pid = self.db.pending_save("u", add("Old"), now - 25 * 3600)
        self.db.pending_save("u", add("Fresh"), now - 1 * 3600)
        out = self.svc.expire(now)
        self.assertEqual(len(out), 1)
        got_id, got_user, got_card = out[0]
        self.assertEqual((got_id, got_user), (pid, "u"))
        self.assertEqual(got_card.title, "Old")
        self.assertEqual(self.db.conn.execute(
            "SELECT status FROM pending WHERE pending_id=?",
            (pid,)).fetchone()["status"], "cancelled")

    def test_nothing_expired_inside_window(self):
        now = 2_000_000.0
        self.db.pending_save("u", add(), now - 23 * 3600)
        self.assertEqual(self.svc.expire(now), [])


if __name__ == "__main__":
    unittest.main()

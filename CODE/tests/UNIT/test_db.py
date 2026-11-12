"""Unit tests for app/storage/db.py: users, processed (idempotency),
pending storage incl. the task_json -> card_json in-place migration,
the lists mirror and the per-user FIFO queue (spec 5.1-5.3)."""
import os
import sqlite3
import tempfile
import unittest

from app.schema import Action, Card
from app.storage.db import DB


def add(title="X") -> Card:
    return Card(action=Action.add_card, title=title)


class TestUsers(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")

    def test_unknown_user_is_none(self):
        self.assertIsNone(self.db.user("nobody"))

    def test_upsert_then_get(self):
        self.db.upsert_user("u1", "m1")
        row = self.db.user("u1")
        self.assertEqual(row["trello_member_id"], "m1")
        self.assertEqual(row["role"], "worker")

    def test_upsert_updates_existing_row(self):
        self.db.upsert_user("u1", "m1", role="admin")
        self.db.upsert_user("u1", "m2")
        row = self.db.user("u1")
        self.assertEqual(row["trello_member_id"], "m2")
        self.assertEqual(row["role"], "worker")   # default on update


class TestProcessed(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")

    def test_mark_then_already_processed(self):
        self.assertFalse(self.db.already_processed("m1"))
        self.db.mark_processed("m1")
        self.assertTrue(self.db.already_processed("m1"))

    def test_double_mark_stays_a_single_row(self):
        self.db.mark_processed("m1")
        self.db.mark_processed("m1")
        n = self.db.conn.execute(
            "SELECT COUNT(*) FROM processed WHERE message_id='m1'").fetchone()[0]
        self.assertEqual(n, 1)


class TestPendingStorage(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")

    def test_save_returns_prefixed_id_and_roundtrips_card(self):
        pid = self.db.pending_save("u", add("Call supplier"), 1000.0)
        self.assertTrue(pid.startswith("P"))
        row = self.db.pending_get(pid)
        self.assertEqual(row.card.action, Action.add_card)
        self.assertEqual(row.card.title, "Call supplier")
        self.assertEqual(row.created_at, 1000.0)
        self.assertEqual(row.status, "pending")

    def test_active_orders_by_created_at(self):
        self.db.pending_save("u", add("second"), 2000.0)
        self.db.pending_save("u", add("first"), 1000.0)
        self.db.pending_save("other", add("x"), 1500.0)
        rows = self.db.pending_active("u")
        self.assertEqual([r.card.title for r in rows], ["first", "second"])

    def test_get_returns_none_once_not_pending(self):
        pid = self.db.pending_save("u", add(), 1000.0)
        self.db.pending_set_status(pid, "applied")
        self.assertIsNone(self.db.pending_get(pid))
        self.assertEqual(self.db.pending_active("u"), [])

    def test_set_status_returns_card_with_parsed_action(self):
        # legacy rows store the old action name "create"
        self.db.conn.execute(
            "INSERT INTO pending (pending_id, user_id, card_json, created_at) "
            "VALUES ('POLD', 'u', ?, 0)",
            ('{"action": "create", "title": "legacy card"}',))
        self.db.conn.commit()
        card = self.db.pending_set_status("POLD", "applied")
        self.assertIs(card.action, Action.add_card)
        self.assertEqual(card.title, "legacy card")

    def test_set_status_unknown_id_returns_none(self):
        self.assertIsNone(self.db.pending_set_status("PNOPE", "applied"))

    def test_due_reminders_interval_and_max(self):
        now = 1_000_000.0
        pid = self.db.pending_save("u", add(), now)
        # never reminded -> due now
        self.assertEqual(len(self.db.pending_due_reminders(now, 7, 3)), 1)
        self.db.pending_bump_reminder(pid, now)
        # not due before the 7 h interval has passed
        self.assertEqual(len(self.db.pending_due_reminders(now + 6 * 3600, 7, 3)), 0)
        # due exactly at the boundary
        self.assertEqual(len(self.db.pending_due_reminders(now + 7 * 3600, 7, 3)), 1)
        # capped at max_reminders
        for i in range(1, 3):
            self.db.pending_bump_reminder(pid, now + i * 7 * 3600)
        self.assertEqual(len(self.db.pending_due_reminders(now + 21 * 3600, 7, 3)), 0)

    def test_expired_uses_ttl_boundary(self):
        now = 2_000_000.0
        self.db.pending_save("u", add("old"), now - 24 * 3600)      # boundary
        self.db.pending_save("u", add("fresh"), now - 23 * 3600)
        expired = self.db.pending_expired(now, 24)
        self.assertEqual([r.card.title for r in expired], ["old"])


class TestTaskJsonMigration(unittest.TestCase):
    """Old DBs store the pending payload in a `task_json` column; DB() must
    rename it to `card_json` in place on startup (blueprint V6)."""

    def test_task_json_column_is_renamed_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.db")
            old = sqlite3.connect(path)
            old.executescript(
                "CREATE TABLE pending (pending_id TEXT PRIMARY KEY, "
                "user_id TEXT NOT NULL, task_json TEXT NOT NULL, "
                "created_at REAL NOT NULL, "
                "reminders_sent INTEGER NOT NULL DEFAULT 0, "
                "last_reminder_at REAL, "
                "status TEXT NOT NULL DEFAULT 'pending');")
            old.execute(
                "INSERT INTO pending (pending_id, user_id, task_json, created_at) "
                "VALUES ('POLD', 'u', ?, 100.0)",
                ('{"action": "create", "title": "legacy"}',))
            old.commit()
            old.close()

            db = DB(path)
            cols = {r[1] for r in db.conn.execute("PRAGMA table_info(pending)")}
            self.assertIn("card_json", cols)
            self.assertNotIn("task_json", cols)
            row = db.pending_active("u")[0]
            self.assertIs(row.card.action, Action.add_card)
            self.assertEqual(row.card.title, "legacy")


class TestListsMirror(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")

    def test_save_get_all(self):
        self.db.list_save("L1", "Review", 100.0)
        self.db.list_save("L2", "Blocked", 200.0)
        self.assertEqual(self.db.list_get("L1")["name"], "Review")
        self.assertEqual([r["list_id"] for r in self.db.list_all()],
                         ["L1", "L2"])

    def test_duplicate_save_is_ignored(self):
        self.db.list_save("L1", "Review", 100.0)
        self.db.list_save("L1", "Renamed", 200.0)
        self.assertEqual(self.db.list_get("L1")["name"], "Review")
        self.assertEqual(len(self.db.list_all()), 1)


class TestQueue(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")

    def test_fifo_per_user(self):
        self.db.queue_push("u", "m1", "first")
        self.db.queue_push("u", "m2", "second")
        self.db.queue_push("u", "m3", "third")
        self.assertEqual(self.db.queue_pop("u"), ("m1", "first"))
        self.assertEqual(self.db.queue_pop("u"), ("m2", "second"))
        self.assertEqual(self.db.queue_pop("u"), ("m3", "third"))
        self.assertIsNone(self.db.queue_pop("u"))

    def test_queues_are_isolated_per_user(self):
        self.db.queue_push("u1", "m1", "for u1")
        self.db.queue_push("u2", "m2", "for u2")
        self.assertEqual(self.db.queue_pop("u1"), ("m1", "for u1"))
        self.assertEqual(self.db.queue_pop("u2"), ("m2", "for u2"))
        self.assertIsNone(self.db.queue_pop("u1"))


if __name__ == "__main__":
    unittest.main()

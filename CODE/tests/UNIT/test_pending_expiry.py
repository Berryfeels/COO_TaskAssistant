"""Integration: the hourly scheduler jobs (spec 5.1.3/5.1.4) through the real
Pipeline - up to 3 reminders every 7 h, never between 02:00-05:00 Berlin,
and cancellation with a notice after the 24 h window."""
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from CODE.tests.SHARED.base import add, chat, incoming, last_reply, make_pipeline, replies_to, run

TZ = ZoneInfo("Europe/Berlin")
DAY = datetime(2026, 11, 2, 10, 0, tzinfo=TZ)     # 10:00 Berlin: daytime
NIGHT = datetime(2026, 11, 2, 3, 0, tzinfo=TZ)    # 03:00 Berlin: quiet hours


class TestReminders(unittest.TestCase):
    def test_three_reminders_every_7_hours_then_stop(self):
        h = make_pipeline()
        now = 1_000_000.0
        pid = h.db.pending_save("u", add("Call supplier"), now - 21 * 3600)
        with mock.patch("app.confirm.pending.now_berlin", lambda: DAY):
            for i in range(4):
                run(h.pipe.reminder_job(now + i * 7 * 3600))
        msgs = replies_to(h.gateway, "u")
        self.assertEqual(len(msgs), 3)               # capped at max_reminders=3
        for m in msgs:
            self.assertIn(f"[{pid}]", m)
            self.assertIn("still awaiting your confirmation", m)
            self.assertIn(f'Reply "OK {pid}"', m)
        row = h.db.conn.execute(
            "SELECT reminders_sent FROM pending WHERE pending_id=?",
            (pid,)).fetchone()
        self.assertEqual(row["reminders_sent"], 3)

    def test_reminder_deferred_in_night_window(self):
        # no reminder is ever sent between 02:00 and 05:00 Berlin; the slot is
        # picked up by a later (daytime) run instead
        h = make_pipeline()
        now = 1_000_000.0
        pid = h.db.pending_save("u", add("X"), now - 7 * 3600)
        with mock.patch("app.confirm.pending.now_berlin", lambda: NIGHT):
            run(h.pipe.reminder_job(now + 7 * 3600))
        self.assertEqual(h.gateway.sent, [])
        row = h.db.conn.execute(
            "SELECT reminders_sent FROM pending WHERE pending_id=?",
            (pid,)).fetchone()
        self.assertEqual(row["reminders_sent"], 0)
        with mock.patch("app.confirm.pending.now_berlin", lambda: DAY):
            run(h.pipe.reminder_job(now + 7 * 3600))
        self.assertEqual(len(replies_to(h.gateway, "u")), 1)

    def test_reminder_mentions_hours_left(self):
        h = make_pipeline()
        now = 1_000_000.0
        h.db.pending_save("u", add("X"), now - 7 * 3600)   # 24-7 = 17 h left
        with mock.patch("app.confirm.pending.now_berlin", lambda: DAY):
            run(h.pipe.reminder_job(now))
        self.assertIn("about 17 h", last_reply(h.gateway, "u"))


class TestPendingExpiry(unittest.TestCase):
    def test_expiry_after_24h_with_cancellation_notice(self):
        h = make_pipeline()
        now = 2_000_000.0
        pid = h.db.pending_save("u", add("Old card"), now - 25 * 3600)
        h.db.pending_save("u", add("Fresh card"), now - 1 * 3600)
        run(h.pipe.expiry_job(now))
        msgs = replies_to(h.gateway, "u")
        self.assertEqual(len(msgs), 1)
        self.assertIn(f"[{pid}]", msgs[0])
        self.assertIn("'Old card'", msgs[0])
        self.assertIn("cancelled", msgs[0])
        status = h.db.conn.execute(
            "SELECT status FROM pending WHERE pending_id=?", (pid,)).fetchone()
        self.assertEqual(status["status"], "cancelled")

    def test_no_card_written_if_expired(self):
        h = make_pipeline()
        now = 2_000_000.0
        h.db.pending_save("u", add("Old card"), now - 25 * 3600)
        run(h.pipe.expiry_job(now))
        self.assertEqual(h.trello.calls, [])         # expiry never writes

    def test_late_confirmation_after_expiry_writes_nothing(self):
        h = make_pipeline(script=[])
        now = 2_000_000.0
        pid = h.db.pending_save("u", add("Old card"), now - 25 * 3600)
        run(h.pipe.expiry_job(now))
        # a confirmation arriving after expiry finds no active pending: the
        # message goes to the LLM as a normal new instruction instead
        h.llm.script.append(chat("too late"))
        incoming(h.pipe, "u", "m1", f"OK {pid}")
        self.assertEqual([c for c in h.trello.calls if c[0] == "apply"], [])
        self.assertEqual(h.trello.cards, [])


if __name__ == "__main__":
    unittest.main()

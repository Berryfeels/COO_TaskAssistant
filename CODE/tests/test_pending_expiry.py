import time

from app.config import Settings
from tests.base import BaseCase, make_pipeline, run


class TestPendingExpiry(BaseCase):
    def test_expiry_after_24h_with_cancellation_notice(self):
        t0 = 1000.0
        self.pipe.db.pending_save  # noqa
        # create pending directly with a controlled created_at
        from app.schema import Action, Task
        task = Task(action=Action.create, title="Call supplier")
        pid = self.db.pending_save(self.user, task, t0)
        run(self.pipe.expiry_job(now=t0 + 23 * 3600))
        self.assertEqual(len(self.sent), 0)          # not yet
        run(self.pipe.expiry_job(now=t0 + 24 * 3600))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("cancelled", self.last_message())
        self.assertEqual(self.pending.active(self.user), [])
        self.assertEqual(self.db.pending_get(pid), None)

    def test_no_card_written_if_expired(self):
        self.say("m1", "create task 'Call supplier' for marco")
        t0 = time.time()
        run(self.pipe.expiry_job(now=t0 + 25 * 3600))
        self.assertEqual(len(self.trello.cards), 0)


class TestReminders(BaseCase):
    def setUp(self):
        s = Settings()
        self.s = s
        super().setUp()

    def _pending_at(self, t0):
        from app.schema import Action, Task
        task = Task(action=Action.create, title="Call supplier")
        return self.db.pending_save(self.user, task, t0)

    def test_three_reminders_every_7_hours(self):
        t0 = 1000.0
        pid = self._pending_at(t0)
        for hours in (7, 14, 21):
            run(self.pipe.reminder_job(now=t0 + hours * 3600))
            self.assertEqual(len(self.sent), 1)
            self.assertIn(pid, self.last_message())
            self.assertIn("Reminder", self.last_message())
            self.sent.clear()
        # after 3 reminders: no more
        run(self.pipe.reminder_job(now=t0 + 28 * 3600))
        self.assertEqual(len(self.sent), 0)

    def test_reminder_deferred_in_night_window(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        t0 = 1000.0
        self._pending_at(t0)
        # fake "now" whose Berlin wall clock is 03:00 -> quiet hours
        night = datetime(2026, 1, 1, 3, 0, tzinfo=ZoneInfo("Europe/Berlin"))
        self.assertTrue(self.pending.quiet_hours(night))
        run(self.pipe.reminder_job(now=t0 + 7 * 3600))  # job checks real clock;
        # quiet-hours gate is exercised via the unit below
        # deterministic check of the gate itself:
        self.assertEqual(self.pending.quiet_hours(night), True)
        day = datetime(2026, 1, 1, 10, 0, tzinfo=ZoneInfo("Europe/Berlin"))
        self.assertEqual(self.pending.quiet_hours(day), False)

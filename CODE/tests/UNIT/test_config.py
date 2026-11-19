"""Unit tests for app/config.py: spec defaults and .env overrides."""
import os
import unittest
from unittest import mock

from app.config import _load, missing_required


class TestSettings(unittest.TestCase):
    def test_defaults_match_the_spec(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            s = _load()
        self.assertEqual(s.timezone, "Europe/Berlin")
        self.assertEqual(s.database_path, "coo.db")
        self.assertEqual(s.pending_ttl_hours, 24)
        self.assertEqual(s.reminder_interval_hours, 7)
        self.assertEqual(s.max_reminders, 3)
        self.assertEqual((s.reminder_quiet_start, s.reminder_quiet_end), (2, 5))
        self.assertEqual(s.simplex_ws_url, "ws://127.0.0.1:5225")
        self.assertEqual(s.mistral_model, "mistral-small-latest")
        self.assertEqual(s.mistral_stt_model, "voxtral-mini-latest")
        self.assertEqual(s.trello_board_sandbox_id, "")

    def test_env_overrides(self):
        env = {"TIMEZONE": "Europe/Rome", "DATABASE_PATH": "/tmp/x.db",
               "PENDING_TTL_HOURS": "12", "PENDING_REMINDER_INTERVAL_HOURS": "5",
               "PENDING_MAX_REMINDERS": "2", "REMINDER_QUIET_START": "3",
               "REMINDER_QUIET_END": "6", "TRELLO_KEY": "k",
               "TRELLO_TOKEN": "t", "TRELLO_BOARD_ID": "b123",
               "TRELLO_BOARD_SANDBOX_ID": "sb1",
               "TRELLO_DEFAULT_LIST_ID": "L1", "MISTRAL_API_KEY": "mk",
               "MISTRAL_MODEL": "mistral-large-latest",
               "MISTRAL_STT_MODEL": "voxtral-small-latest",
               "SIMPLEX_WS_URL": "ws://10.0.0.1:9999"}
        with mock.patch.dict(os.environ, env, clear=True):
            s = _load()
        self.assertEqual(s.timezone, "Europe/Rome")
        self.assertEqual(s.database_path, "/tmp/x.db")
        self.assertEqual(s.pending_ttl_hours, 12)
        self.assertEqual(s.reminder_interval_hours, 5)
        self.assertEqual(s.max_reminders, 2)
        self.assertEqual((s.reminder_quiet_start, s.reminder_quiet_end), (3, 6))
        self.assertEqual(s.trello_key, "k")
        self.assertEqual(s.trello_token, "t")
        self.assertEqual(s.trello_board_id, "b123")
        self.assertEqual(s.trello_board_sandbox_id, "sb1")
        self.assertEqual(s.trello_default_list_id, "L1")
        self.assertEqual(s.mistral_api_key, "mk")
        self.assertEqual(s.mistral_model, "mistral-large-latest")
        self.assertEqual(s.mistral_stt_model, "voxtral-small-latest")
        self.assertEqual(s.simplex_ws_url, "ws://10.0.0.1:9999")


class TestMissingRequired(unittest.TestCase):
    def test_all_four_missing_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(missing_required(_load()),
                             ["TRELLO_KEY", "TRELLO_TOKEN", "TRELLO_BOARD_ID",
                              "MISTRAL_API_KEY"])

    def test_none_missing_when_set(self):
        env = {"TRELLO_KEY": "k", "TRELLO_TOKEN": "t", "TRELLO_BOARD_ID": "b",
               "MISTRAL_API_KEY": "m"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(missing_required(_load()), [])

    def test_partial_config_reports_only_the_missing_ones(self):
        env = {"TRELLO_KEY": "k", "MISTRAL_API_KEY": "m"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(missing_required(_load()),
                             ["TRELLO_TOKEN", "TRELLO_BOARD_ID"])


if __name__ == "__main__":
    unittest.main()

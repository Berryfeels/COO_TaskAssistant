"""Unit/integration tests for app/gateway/onboarding.py: the admit gate that
maps a SimpleX contact to a Trello member before any message reaches the
pipeline (spec section 8, user mapping).

Non-exact name matches are never mapped silently: the bot double-checks
("Are you Stefano Di Puma?" / "Several members match ... which one?") and
only an affirmative or a candidate-naming reply saves the mapping.
"""
import unittest

from app.gateway.onboarding import Onboarding
from app.pipeline import Gateway
from app.storage.db import DB
from CODE.tests.SHARED.base import FakeTrello, last_reply, run


def setup(members=None):
    db = DB(":memory:")
    trello = FakeTrello()
    if members is not None:
        trello.members = members
    gw = Gateway()
    return Onboarding(db, trello, gw), db, gw


class TestOnboarding(unittest.TestCase):
    def test_unknown_contact_is_asked_for_name_and_consumed(self):
        onb, db, gw = setup()
        admitted = run(onb.admit("newbie", "m1", "hello"))
        self.assertFalse(admitted)
        self.assertIn("What is your name", last_reply(gw, "newbie"))
        self.assertIsNone(db.user("newbie"))

    def test_first_name_match_asks_are_you_confirmation(self):
        onb, db, gw = setup()
        run(onb.admit("newbie", "m1", "hi"))
        # "Stefano" matches "Stefano Di Puma" only on the first token: the
        # bot must double-check instead of mapping silently
        admitted = run(onb.admit("newbie", "m2", "Stefano"))
        self.assertFalse(admitted)               # the name itself is not a card
        self.assertIn("Are you Stefano Di Puma?", last_reply(gw, "newbie"))
        self.assertIsNone(db.user("newbie"))     # not mapped before confirmation
        # the affirmative reply saves the mapping
        run(onb.admit("newbie", "m3", "yes"))
        row = db.user("newbie")
        self.assertEqual(row["trello_member_id"], "m1")
        self.assertIn("Thanks, Stefano Di Puma", last_reply(gw, "newbie"))
        # from now on messages pass the gate
        self.assertTrue(run(onb.admit("newbie", "m4", "add a card")))

    def test_confirmation_can_be_declined_and_name_reasked(self):
        onb, db, gw = setup()
        run(onb.admit("newbie", "m1", "hi"))
        run(onb.admit("newbie", "m2", "Stefano"))
        self.assertIn("Are you Stefano Di Puma?", last_reply(gw, "newbie"))
        run(onb.admit("newbie", "m3", "no"))
        self.assertIn("what is your name", last_reply(gw, "newbie"))
        self.assertIsNone(db.user("newbie"))
        # the exact full name maps immediately, without another question
        run(onb.admit("newbie", "m4", "Stefano Di Puma"))
        self.assertEqual(db.user("newbie")["trello_member_id"], "m1")

    def test_typo_stephan_asks_confirmation(self):
        onb, db, gw = setup()
        run(onb.admit("newbie", "m1", "hi"))
        run(onb.admit("newbie", "m2", "Stephan"))   # fuzzy near-match
        self.assertIn("Are you Stefano Di Puma?", last_reply(gw, "newbie"))
        self.assertIsNone(db.user("newbie"))
        run(onb.admit("newbie", "m3", "sì"))
        self.assertEqual(db.user("newbie")["trello_member_id"], "m1")

    def test_near_duplicate_members_are_listed_not_guessed(self):
        onb, db, gw = setup(members=[{"id": "m1", "fullName": "Stefano Di Puma"},
                                     {"id": "m9", "fullName": "Stephan Mueller"},
                                     {"id": "m2", "fullName": "Marco Rossi"}])
        run(onb.admit("newbie", "m1", "hi"))
        run(onb.admit("newbie", "m2", "Stefano"))   # matches both Stefanos
        msg = last_reply(gw, "newbie")
        self.assertIn("Several members match", msg)
        self.assertIn("Stefano Di Puma", msg)
        self.assertIn("Stephan Mueller", msg)
        self.assertIsNone(db.user("newbie"))        # never guesses
        run(onb.admit("newbie", "m3", "yes"))       # ambiguous: ask which one
        self.assertIn("Which one are you", last_reply(gw, "newbie"))
        self.assertIsNone(db.user("newbie"))
        run(onb.admit("newbie", "m4", "Stephan Mueller"))   # names the candidate
        self.assertEqual(db.user("newbie")["trello_member_id"], "m9")

    def test_unknown_name_is_reasked(self):
        onb, db, gw = setup()
        run(onb.admit("newbie", "m1", "hi"))
        admitted = run(onb.admit("newbie", "m2", "Zorro"))
        self.assertFalse(admitted)
        self.assertIn("can't find", last_reply(gw, "newbie"))
        self.assertIsNone(db.user("newbie"))

    def test_full_name_match_is_case_insensitive(self):
        onb, db, gw = setup()
        run(onb.admit("newbie", "m1", "hi"))
        run(onb.admit("newbie", "m2", "  STEFANO DI PUMA "))
        self.assertEqual(db.user("newbie")["trello_member_id"], "m1")

    def test_ambiguous_first_name_asks_for_disambiguation(self):
        onb, db, gw = setup(members=[{"id": "m1", "fullName": "Marco Rossi"},
                                     {"id": "m2", "fullName": "Marco Bianchi"},
                                     {"id": "m3", "fullName": "Stefano Di Puma"}])
        run(onb.admit("newbie", "m1", "hi"))
        run(onb.admit("newbie", "m2", "Marco"))
        msg = last_reply(gw, "newbie")
        self.assertIn("Several members match", msg)
        self.assertIn("Marco Rossi", msg)
        self.assertIn("Marco Bianchi", msg)
        self.assertIsNone(db.user("newbie"))
        # the full name disambiguates
        run(onb.admit("newbie", "m3", "Marco Bianchi"))
        self.assertEqual(db.user("newbie")["trello_member_id"], "m2")

    def test_board_failure_is_reported_and_retryable(self):
        onb, db, gw = setup()
        run(onb.admit("newbie", "m1", "hi"))
        def boom():
            raise RuntimeError("HTTP 400 invalid id")
        onb.trello.list_members = boom          # board unreachable/misconfigured
        admitted = run(onb.admit("newbie", "m2", "Stefano"))
        self.assertFalse(admitted)              # consumed, but not mapped
        self.assertIn("can't reach the Trello board", last_reply(gw, "newbie"))
        self.assertIsNone(db.user("newbie"))
        # the board comes back: the next message retries the match
        onb.trello.list_members = lambda: list(onb.trello.members)
        run(onb.admit("newbie", "m3", "Stefano"))
        self.assertIn("Are you Stefano Di Puma?", last_reply(gw, "newbie"))
        run(onb.admit("newbie", "m4", "yes"))
        self.assertEqual(db.user("newbie")["trello_member_id"], "m1")

    def test_mapping_survives_restart(self):
        onb, db, gw = setup()
        run(onb.admit("newbie", "m1", "hi"))
        run(onb.admit("newbie", "m2", "Stefano"))
        run(onb.admit("newbie", "m3", "yes"))
        # a fresh Onboarding (bot restart) reads the persisted users table
        onb2 = Onboarding(db, FakeTrello(), gw)
        self.assertEqual(onb2.member_id("newbie"), "m1")
        self.assertTrue(run(onb2.admit("newbie", "m4", "add a card")))
        self.assertEqual(len(gw.sent), 3)       # welcome + question + thanks


if __name__ == "__main__":
    unittest.main()

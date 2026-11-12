"""Unit/integration tests for app/gateway/onboarding.py: the admit gate that
maps a SimpleX contact to a Trello member before any message reaches the
pipeline (spec section 8, user mapping)."""
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

    def test_name_reply_maps_contact_to_member(self):
        onb, db, gw = setup()
        run(onb.admit("newbie", "m1", "hi"))
        admitted = run(onb.admit("newbie", "m2", "Stefano"))
        self.assertFalse(admitted)               # the name itself is not a card
        row = db.user("newbie")
        self.assertEqual(row["trello_member_id"], "m1")
        self.assertIn("Thanks, Stefano Di Puma", last_reply(gw, "newbie"))
        # from now on messages pass the gate
        self.assertTrue(run(onb.admit("newbie", "m3", "add a card")))

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

    def test_mapping_survives_restart(self):
        onb, db, gw = setup()
        run(onb.admit("newbie", "m1", "hi"))
        run(onb.admit("newbie", "m2", "Stefano"))
        # a fresh Onboarding (bot restart) reads the persisted users table
        onb2 = Onboarding(db, FakeTrello(), gw)
        self.assertEqual(onb2.member_id("newbie"), "m1")
        self.assertTrue(run(onb2.admit("newbie", "m3", "add a card")))
        self.assertEqual(len(gw.sent), 2)       # no re-asking after restart


if __name__ == "__main__":
    unittest.main()

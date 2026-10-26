import unittest

from tests.base import run

from app.gateway.onboarding import Onboarding
from app.storage.db import DB
from app.trello.client import FakeTrello


class GatewayStub:
    """Captures what the bot sends to the contact."""

    def __init__(self):
        self.replies: list[tuple[str, str]] = []

    async def send(self, user_id, text):
        self.replies.append((user_id, text))


class ThreeMarcos(FakeTrello):
    def list_members(self):
        return [{"id": "m1", "fullName": "Marco Rossi"},
                {"id": "m2", "fullName": "Marco Bianchi"},
                {"id": "m3", "fullName": "Anna Verdi"}]


def make(trello=None):
    db = DB(":memory:")
    trello = trello or FakeTrello()          # default members: Marco m1, Anna m2
    gateway = GatewayStub()
    onboard = Onboarding(db, trello, gateway)
    return db, trello, gateway, onboard


class TestOnboarding(unittest.TestCase):
    def test_unknown_contact_is_asked_then_mapped(self):
        db, trello, gateway, onboard = make()
        # first message: question, consumed, nothing reaches the pipeline
        ok = run(onboard.admit("c1", "m1", "hello bot"))
        self.assertFalse(ok)
        self.assertIn("name on the board", gateway.replies[-1][1])
        # reply with a first name: matched, mapped, welcomed
        ok = run(onboard.admit("c1", "m2", "Marco"))
        self.assertFalse(ok)                     # the name is not a task
        self.assertIn("Thanks, Marco", gateway.replies[-1][1])
        row = db.user("c1")
        self.assertEqual(row["trello_member_id"], "m1")
        # from now on the contact is admitted to the pipeline
        self.assertTrue(run(onboard.admit("c1", "m3", "create task 'X'")))

    def test_unknown_name_is_reasked(self):
        db, trello, gateway, onboard = make()
        run(onboard.admit("c1", "m1", "hi"))
        run(onboard.admit("c1", "m2", "Paolo"))
        self.assertIn("can't find", gateway.replies[-1][1])
        self.assertIsNone(db.user("c1"))          # still unmapped
        run(onboard.admit("c1", "m3", "anna"))
        self.assertEqual(db.user("c1")["trello_member_id"], "m2")

    def test_mapping_survives_pipeline_restart(self):
        db, trello, gateway, onboard = make()
        run(onboard.admit("c1", "m1", "hi"))      # greeting -> asked
        run(onboard.admit("c1", "m2", "Marco"))  # name -> mapped
        # a fresh Onboarding over the same (persistent) DB admits at once
        onboard2 = Onboarding(db, trello, gateway)
        self.assertTrue(run(onboard2.admit("c1", "m9", "anything")))

    def test_full_name_disambiguation_multiple_first_names(self):
        db, trello, gateway, onboard = make(ThreeMarcos())
        run(onboard.admit("c1", "m1", "hello"))
        run(onboard.admit("c1", "m2", "Marco"))
        self.assertIn("Several members match", gateway.replies[-1][1])
        self.assertIsNone(db.user("c1"))
        ok = run(onboard.admit("c1", "m3", "Marco Bianchi"))
        self.assertFalse(ok)
        self.assertEqual(db.user("c1")["trello_member_id"], "m2")

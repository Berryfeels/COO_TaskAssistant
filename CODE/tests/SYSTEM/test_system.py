"""System tests: the live path end to end - a real SimpleX newChatItems
frame -> SimpleXGateway.on_chat_event -> Pipeline.handle_incoming -> LLM ->
Trello write -> reply pushed back onto the WebSocket as "@<contact> <text>".

Only the network edges are faked: the SimpleX socket, the Trello REST API
and the Mistral API. Gateway, Pipeline, Onboarding, PendingService, DB and
all render/similarity/dates code are the production objects (blueprint 9.1:
"the same call the tests make is made by real user messages").
"""
import json
import pathlib
import py_compile
import unittest

from app.config import Settings
from app.confirm.pending import PendingService
from app.gateway.onboarding import Onboarding
from app.gateway.simplex import SimpleXGateway
from app.pipeline import Pipeline
from app.schema import Action, Card
from app.storage.db import DB
from CODE.tests.SHARED.base import FakeLLM, FakeTrello, run, sx_frame


class FakeSocket:
    """Stands in for the open WebSocket to the SimpleX CLI."""

    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


def build(script=(), with_onboarding=False):
    db = DB(":memory:")
    trello = FakeTrello()
    llm = FakeLLM(script)
    settings = Settings()
    pending = PendingService(db, settings)
    gw = SimpleXGateway("ws://fake")
    pipe = Pipeline(db, trello, llm, pending, gw, settings)
    gw.pipeline = pipe
    if with_onboarding:
        gw.admit = Onboarding(db, trello, gw).admit
    sock = FakeSocket()
    gw._ws = sock
    return pipe, db, trello, llm, gw, sock


def deliver(gw, body, display="Stefano", item_id=1):
    """Push one raw server frame through the real receive path."""
    run(gw._on_raw(json.dumps(sx_frame(body, display, item_id))))


class TestEndToEndThroughGateway(unittest.TestCase):
    def test_message_to_trello_and_reply_over_socket(self):
        pipe, db, trello, llm, gw, sock = build(
            script=[Card(action=Action.add_card, title="Call supplier")])
        deliver(gw, "please add a card: call the supplier", item_id=101)
        # the frame became a pipeline input with the right ids
        self.assertEqual(llm.calls[0][0], "please add a card: call the supplier")
        self.assertEqual(llm.calls[0][1]["user_id"], "Stefano")
        self.assertEqual(llm.calls[0][1]["source_message_id"], "101")
        # Trello was written immediately (no confirmation loop)
        self.assertEqual(len(trello.cards), 1)
        self.assertEqual(trello.cards[0]["name"], "Call supplier")
        self.assertEqual(trello.cards[0]["idList"], "Ltodo")
        # the reply went to the sink AND onto the websocket as a chat command
        self.assertEqual(gw.sent[-1],
                         ("Stefano", "Done: 'Call supplier' created in Trello."))
        self.assertEqual(sock.sent[-1]["cmd"],
                         "@Stefano Done: 'Call supplier' created in Trello.")
        self.assertIn("corrId", sock.sent[-1])
        self.assertTrue(db.already_processed("101"))

    def test_same_event_redelivered_is_processed_once(self):
        pipe, db, trello, llm, gw, sock = build(
            script=[Card(action=Action.add_card, title="X")])
        deliver(gw, "add X", item_id=55)
        deliver(gw, "add X", item_id=55)          # SimpleX redelivery
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(len(trello.cards), 1)
        self.assertEqual(len(sock.sent), 1)

    def test_full_card_lifecycle_over_chat(self):
        pipe, db, trello, llm, gw, sock = build(script=[
            Card(action=Action.add_card, title="Prepare demo"),
            Card(action=Action.query),
            Card(action=Action.complete, target_card_id="?"),
            Card(action=Action.archive, target_card_id="?"),
            Card(action=Action.query),
        ])
        deliver(gw, "add: prepare the demo", item_id=1)
        card_id = trello.cards[0]["id"]
        for c in llm.script:                      # resolve the board-assigned id
            if c.target_card_id == "?":
                c.target_card_id = card_id

        deliver(gw, "what are my open cards?", item_id=2)
        self.assertIn("- Prepare demo [To-do]", gw.sent[-1][1])

        deliver(gw, "mark prepare demo as done", item_id=3)
        self.assertTrue(trello.cards[0]["dueComplete"])
        self.assertIn("updated", gw.sent[-1][1])

        deliver(gw, "archive prepare demo", item_id=4)
        self.assertTrue(trello.cards[0]["closed"])

        deliver(gw, "what are my open cards?", item_id=5)
        self.assertEqual(gw.sent[-1][1], "You have no open cards.")
        # every frame produced exactly one reply on the socket
        self.assertEqual(len(sock.sent), 5)
        self.assertTrue(all(s["cmd"].startswith("@Stefano ")
                            for s in sock.sent))

    def test_onboarding_gate_then_pipeline(self):
        pipe, db, trello, llm, gw, sock = build(
            script=[Card(action=Action.add_card, title="Call supplier")],
            with_onboarding=True)
        deliver(gw, "hello", display="Newbie", item_id=1)
        self.assertEqual(len(llm.calls), 0)                  # gate consumed it
        self.assertIn("What is your name", gw.sent[-1][1])
        self.assertIsNone(db.user("Newbie"))

        deliver(gw, "Stefano", display="Newbie", item_id=2)
        self.assertIsNotNone(db.user("Newbie"))              # mapped to m1
        self.assertEqual(db.user("Newbie")["trello_member_id"], "m1")
        self.assertEqual(len(llm.calls), 0)                  # name is not a card

        deliver(gw, "add: call the supplier", display="Newbie", item_id=3)
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(len(trello.cards), 1)
        self.assertEqual(sock.sent[-1]["cmd"],
                         "@Newbie Done: 'Call supplier' created in Trello.")

    def test_two_users_are_isolated(self):
        pipe, db, trello, llm, gw, sock = build(script=[
            Card(action=Action.add_card, title="A"),
            Card(action=Action.add_card, title="B"),
            Card(action=Action.query),
        ])
        deliver(gw, "add A", display="Stefano", item_id=1)
        deliver(gw, "add B", display="Marco", item_id=2)
        deliver(gw, "my cards?", display="Marco", item_id=3)
        self.assertEqual([t for t, _ in llm.calls],
                         ["add A", "add B", "my cards?"])
        self.assertEqual(llm.calls[1][1]["user_id"], "Marco")
        # replies went to the right contacts over the socket
        self.assertEqual(gw.sent[-1][0], "Marco")
        self.assertEqual(sock.sent[-1]["cmd"].split(" ", 1)[0], "@Marco")
        for mid in ("1", "2", "3"):
            self.assertTrue(db.already_processed(mid))

    def test_scheduler_notification_goes_over_the_socket(self):
        pipe, db, trello, llm, gw, sock = build()
        now = 3_000_000.0
        pid = db.pending_save("Stefano", Card(action=Action.add_card,
                                               title="Old card"),
                              now - 25 * 3600)
        run(pipe.expiry_job(now))
        self.assertIn("cancelled", gw.sent[-1][1])
        self.assertIn(f"[{pid}]", sock.sent[-1]["cmd"])
        self.assertTrue(sock.sent[-1]["cmd"].startswith("@Stefano "))
        status = db.conn.execute(
            "SELECT status FROM pending WHERE pending_id=?", (pid,)).fetchone()
        self.assertEqual(status["status"], "cancelled")
        self.assertEqual(trello.calls, [])                   # expiry never writes

    def test_pipeline_error_does_not_kill_the_gateway(self):
        pipe, db, trello, llm, gw, sock = build()

        class Boom:
            async def handle_incoming(self, *a):
                raise RuntimeError("boom")

        gw.pipeline = Boom()
        deliver(gw, "anything", item_id=9)                   # must not raise
        gw.pipeline = pipe
        llm.script.append(Card(action=Action.chat, reply="still alive"))
        deliver(gw, "hi", item_id=10)                        # still consuming
        self.assertEqual(gw.sent[-1][1], "still alive")


class TestSourcesCompile(unittest.TestCase):
    def test_all_app_and_script_modules_compile(self):
        root = pathlib.Path(__file__).resolve().parents[2]   # CODE/
        files = list((root / "app").rglob("*.py")) + \
                list((root / "scripts").rglob("*.py"))
        self.assertGreater(len(files), 15)
        for p in files:
            py_compile.compile(str(p), doraise=True)


if __name__ == "__main__":
    unittest.main()

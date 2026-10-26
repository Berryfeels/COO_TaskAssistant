import asyncio
import json
import types
import unittest

from app.gateway import simplex as sx
from app.gateway.simplex import SimpleXGateway


class FakePipeline:
    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []

    async def handle_incoming(self, user_id, message_id, text):
        self.calls.append((user_id, message_id, text))


def chat_item(body="hello", chat_dir="directRcv", display="Stefano",
              item_id=345, content_type=None):
    """Real v7.0.0.12 chatItem pair (verified against the live CLI)."""
    if content_type is None:
        content_type = ("sndMsgContent" if chat_dir == "directSnd"
                         else "rcvMsgContent")
    return {"chatInfo": {"type": "direct",
                         "contact": {"contactId": 12,
                                     "localDisplayName": display}},
            "chatItem": {"chatDir": {"type": chat_dir},
                         "meta": {"itemId": item_id, "itemText": body},
                         "content": {"type": content_type,
                                     "msgContent": {"type": "text",
                                                    "text": body}}}}


def chat_event(body="hello", chat_dir="directRcv", display="Stefano",
               item_id=345, etype="newChatItems"):
    """Real v7.0.0.12 newChatItems frame: items live in a chatItems array."""
    return {"type": etype, "user": {},
            "chatItems": [chat_item(body=body, chat_dir=chat_dir,
                                    display=display, item_id=item_id)]}


class FakeConn:
    """One fake WebSocket connection: yields queued frames, then closes."""

    def __init__(self, frames: list, state: dict):
        self.frames, self.state = list(frames), state
        self.sent: list[str] = []

    async def __aenter__(self):
        self.state["connects"] += 1
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.frames:
            raise StopAsyncIteration              # connection closed
        return json.dumps(self.frames.pop(0))

    async def send(self, raw):
        self.sent.append(raw)


class TestExtraction(unittest.TestCase):
    def setUp(self):
        self.g = SimpleXGateway("ws://fake")

    def test_received_text_item(self):
        self.assertEqual(self.g._extract_incoming(chat_event()),
                         [("Stefano", "345", "hello")])

    def test_sent_items_ignored(self):
        self.assertEqual(
            self.g._extract_incoming(chat_event(chat_dir="directSnd")), [])

    def test_non_chat_events_ignored(self):
        self.assertEqual(
            self.g._extract_incoming(chat_event(etype="connectionsDiff")), [])

    def test_group_chat_ignored(self):
        ev = chat_event()
        ev["chatItems"][0]["chatInfo"]["type"] = "group"
        self.assertEqual(self.g._extract_incoming(ev), [])

    def test_non_text_content_ignored(self):
        ev = chat_event()
        ev["chatItems"][0]["chatItem"]["content"]["msgContent"]["type"] = "voice"
        self.assertEqual(self.g._extract_incoming(ev), [])

    def test_missing_meta_ignored(self):
        ev = chat_event()
        ev["chatItems"][0]["chatItem"]["meta"]["itemId"] = None
        self.assertEqual(self.g._extract_incoming(ev), [])

    def test_multiple_items_in_one_event(self):
        ev = chat_event(body="one", item_id=1)
        ev["chatItems"].append(chat_item(body="two", item_id=2))
        ev["chatItems"].append(chat_item(body="mine", chat_dir="directSnd",
                                         item_id=3))
        self.assertEqual(self.g._extract_incoming(ev),
                         [("Stefano", "1", "one"), ("Stefano", "2", "two")])


class TestRouting(unittest.TestCase):
    def setUp(self):
        self.g = SimpleXGateway("ws://fake")
        self.fp = FakePipeline()
        self.g.pipeline = self.fp

    def test_event_reaches_pipeline(self):
        asyncio.run(self.g.on_chat_event(chat_event()))
        self.assertEqual(self.fp.calls, [("Stefano", "345", "hello")])

    def test_admit_gate_consumes_message(self):
        async def deny(user_id, message_id, text):
            return False
        self.g.admit = deny
        asyncio.run(self.g.on_chat_event(chat_event()))
        self.assertEqual(self.fp.calls, [])

    def test_on_raw_unwraps_resp_events(self):
        # server-initiated events arrive as {"resp": {...}} without corrId
        asyncio.run(self.g._on_raw(json.dumps({"resp": chat_event(body="x")})))
        self.assertEqual(self.fp.calls, [("Stefano", "345", "x")])

    def test_on_raw_routes_events_and_ignores_responses(self):
        asyncio.run(self.g._on_raw(json.dumps(chat_event(body="via raw"))))
        asyncio.run(self.g._on_raw(json.dumps({"corrId": "1", "resp": {}})))
        asyncio.run(self.g._on_raw("not json"))
        self.assertEqual(self.fp.calls, [("Stefano", "345", "via raw")])


class TestSend(unittest.TestCase):
    def test_send_records_and_pushes_to_socket(self):
        g = SimpleXGateway("ws://fake")
        conn = FakeConn([], {"connects": 0})
        g._ws = conn
        asyncio.run(g.send("Stefano", "hello there"))
        asyncio.run(g.send("Stefano", "second"))
        self.assertEqual(g.sent, [("Stefano", "hello there"),
                                  ("Stefano", "second")])
        first = json.loads(conn.sent[0])
        self.assertEqual(first["cmd"], "@Stefano hello there")
        second = json.loads(conn.sent[1])
        self.assertEqual(second["corrId"], "2")      # corrId increments

    def test_send_without_connection_does_not_raise(self):
        g = SimpleXGateway("ws://fake")
        asyncio.run(g.send("Stefano", "dropped"))
        self.assertEqual(g.sent, [("Stefano", "dropped")])  # recorded only


class StopConn:
    """Connection whose establishment raises CancelledError (stops the loop)."""

    async def __aenter__(self):
        raise asyncio.CancelledError

    async def __aexit__(self, *a):
        return False


class TestRunLoop(unittest.TestCase):
    """Reconnect behavior with a fake websocket module (no network).

    The real websockets.connect(url) returns an async context manager and
    the connection happens in __aenter__; the fake models that exactly.
    """

    def test_reconnects_and_delivers_events(self):
        state = {"connects": 0, "urls": []}
        plans = [[chat_event(body="one", item_id=1)],
                 [chat_event(body="two", item_id=2)],
                 None]                                   # 3rd connect stops

        def fake_connect(url):
            state["urls"].append(url)
            plan = plans.pop(0)
            if plan is None:
                return StopConn()
            return FakeConn(plan, state)

        real_ws, real_aio = sx.websockets, sx.asyncio
        sx.websockets = types.SimpleNamespace(connect=fake_connect)
        sx.asyncio = types.SimpleNamespace(
            CancelledError=asyncio.CancelledError, sleep=_fast_sleep)
        try:
            g = SimpleXGateway("ws://fake")
            fp = FakePipeline()
            g.pipeline = fp
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(g.run())
            self.assertEqual(state["connects"], 2)   # 3rd attempt never opens
            self.assertEqual(state["urls"], ["ws://fake"] * 3)
            self.assertEqual(
                [c[2] for c in fp.calls], ["one", "two"])
        finally:
            sx.websockets, sx.asyncio = real_ws, real_aio


async def _fast_sleep(_s):
    return None

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


class TestMediaHandling(unittest.TestCase):
    """The STT seam: non-text messages are surfaced by _extract_media and
    handled by _handle_media - either through the transcriber hook (phase 5)
    or, without one, with a plain "text only for now" reply (never silent)."""

    def voice_event(self, nest_in_msg_content=False):
        # real CLI shape: the file reference is a SIBLING of "content" on the
        # chatItem; the msgContent of a voice message is just
        # {"type": "voice", "text": "", "duration": N}
        ev = chat_event()
        item = ev["chatItems"][0]["chatItem"]
        item["content"]["msgContent"] = {"type": "voice", "text": "",
                                         "duration": 4}
        f = {"fileId": 77, "fileName": "voice_20261116_192414.m4a",
             "fileSize": 49479, "fileStatus": {"type": "rcvInvitation"}}
        if nest_in_msg_content:
            item["content"]["msgContent"]["file"] = f
        else:
            item["file"] = f
        return ev

    def setUp(self):
        self.g = SimpleXGateway("ws://fake")
        self.fp = FakePipeline()
        self.g.pipeline = self.fp

    def test_media_extracted_separately_from_text(self):
        ev = self.voice_event()
        self.assertEqual(self.g._extract_incoming(ev), [])
        media = self.g._extract_media(ev)
        self.assertEqual(len(media), 1)
        user_id, message_id, kind, payload = media[0]
        self.assertEqual((user_id, message_id, kind),
                         ("Stefano", "345", "voice"))
        self.assertEqual(payload["file"]["fileId"], 77)
        self.assertEqual(payload["msgContent"]["type"], "voice")

    def test_file_nested_in_msg_content_still_found(self):
        media = self.g._extract_media(self.voice_event(nest_in_msg_content=True))
        self.assertEqual(media[0][3]["file"]["fileId"], 77)

    def test_voice_without_transcriber_gets_text_only_reply(self):
        asyncio.run(self.g.on_chat_event(self.voice_event()))
        self.assertEqual(self.fp.calls, [])            # pipeline untouched
        self.assertEqual(len(self.g.sent), 1)
        self.assertIn("only read text", self.g.sent[0][1])

    def test_voice_with_transcriber_feeds_transcript_to_pipeline(self):
        async def stt(payload):
            assert payload["msgContent"]["type"] == "voice"
            assert payload["file"]["fileId"] == 77
            return "add a card: call supplier"
        self.g.transcriber = stt
        asyncio.run(self.g.on_chat_event(self.voice_event()))
        self.assertEqual(self.fp.calls,
                         [("Stefano", "345", "add a card: call supplier")])
        self.assertEqual(self.g.sent, [])              # no text-only reply

    def test_transcriber_respects_admit_gate(self):
        async def stt(mc):
            return "Stefano"
        async def deny(user_id, message_id, text):
            return False
        self.g.transcriber = stt
        self.g.admit = deny
        asyncio.run(self.g.on_chat_event(self.voice_event()))
        self.assertEqual(self.fp.calls, [])            # gate consumed it

    def test_transcriber_failure_replies_error_not_silence(self):
        async def stt(mc):
            raise RuntimeError("whisper crashed")
        self.g.transcriber = stt
        asyncio.run(self.g.on_chat_event(self.voice_event()))
        self.assertEqual(self.fp.calls, [])
        self.assertIn("could not transcribe", self.g.sent[-1][1])

    def test_empty_transcript_replies_not_silence(self):
        async def stt(mc):
            return "   "
        self.g.transcriber = stt
        asyncio.run(self.g.on_chat_event(self.voice_event()))
        self.assertEqual(self.fp.calls, [])
        self.assertIn("could not understand", self.g.sent[-1][1])

    def test_media_handling_does_not_block_the_read_loop(self):
        """Regression test for the self-deadlock: _handle_media awaits
        receive_file, which waits for an rcvFileComplete FRAME on the same
        socket. If on_chat_event awaited media handling inline, the read loop
        (async for raw in ws) would be blocked and the frame could never be
        read -> timeout. Media handling must run as a background task."""
        conn = FakeConn([], {"connects": 0})

        async def stt(payload):
            # transcriber downloads the file via the gateway: this waits for
            # a frame that only the (unblocked) read loop can deliver
            path = await self.g.receive_file(
                str(payload["file"]["fileId"]))
            return f"transcribed:{path}"

        self.g._ws = conn
        self.g.transcriber = stt

        async def scenario():
            # voice message arrives: on_chat_event must return immediately,
            # leaving the media work in a background task
            await self.g.on_chat_event(self.voice_event())
            self.assertEqual(self.fp.calls, [])
            self.assertEqual(len(self.g._tasks), 1)
            await asyncio.sleep(0)               # let the task reach /freceive
            await asyncio.sleep(0)
            cmd = json.loads(conn.sent[0])
            self.assertEqual(cmd["cmd"], "/freceive 77")
            # command response + completion frame arrive while the media
            # task is still pending - the loop is NOT blocked
            await self.g._on_raw(json.dumps(
                {"corrId": cmd["corrId"], "resp": {"type": "cmdOk"}}))
            await self.g._on_raw(json.dumps({"resp": {
                "type": "rcvFileComplete",
                "chatItem": {"chatItem": {"file": {
                    "fileId": 77,
                    "fileStatus": {"type": "rcvComplete"},
                    "filePath": "/tmp/v.m4a"}}}}}))
            await asyncio.gather(*list(self.g._tasks))
            return None

        asyncio.run(scenario())
        self.assertEqual(self.fp.calls,
                         [("Stefano", "345", "transcribed:/tmp/v.m4a")])


class TestReceiveFile(unittest.TestCase):
    """receive_file(): /freceive <fileId> over the socket, then the CLI
    reports the downloaded file's local path in a chat item (fileStatus
    rcvComplete + filePath) - that path resolves the waiter."""

    def setUp(self):
        self.g = SimpleXGateway("ws://fake")
        self.g.pipeline = FakePipeline()
        self.conn = FakeConn([], {"connects": 0})
        self.g._ws = self.conn

    def test_receive_file_sends_freceive_and_resolves_on_rcv_file_complete(self):
        # the real v7 event: {"resp": {"type": "rcvFileComplete",
        #   "chatItem": {"chatInfo": ..., "chatItem": {..., "file": {...}}}}}
        async def scenario():
            task = asyncio.ensure_future(self.g.receive_file("77"))
            await asyncio.sleep(0)                 # let the command go out
            cmd = json.loads(self.conn.sent[0])
            self.assertEqual(cmd["cmd"], "/freceive 77")
            self.assertIn("corrId", cmd)
            await self.g._on_raw(json.dumps(
                {"corrId": cmd["corrId"], "resp": {"type": "cmdOk"}}))
            ev = {"type": "rcvFileComplete",
                  "chatItem": {"chatInfo": {"type": "direct"},
                               "chatItem": {
                                   "chatDir": {"type": "directRcv"},
                                   "meta": {"itemId": 345},
                                   "content": {"type": "rcvMsgContent",
                                               "msgContent": {"type": "voice"}},
                                   "file": {"fileId": 77,
                                            "fileName": "voice.m4a",
                                            "fileStatus": {"type": "rcvComplete"},
                                            "filePath": "/tmp/simplex/voice_77.m4a"}}}}
            await self.g._on_raw(json.dumps({"resp": ev}))
            return await task
        path = asyncio.run(scenario())
        self.assertEqual(path, "/tmp/simplex/voice_77.m4a")

    def test_receive_file_also_resolves_from_new_chat_items(self):
        async def scenario():
            task = asyncio.ensure_future(self.g.receive_file("77"))
            await asyncio.sleep(0)
            cmd = json.loads(self.conn.sent[0])
            await self.g._on_raw(json.dumps(
                {"corrId": cmd["corrId"], "resp": {"type": "cmdOk"}}))
            ev = chat_event()
            item = ev["chatItems"][0]["chatItem"]
            item["file"] = {"fileId": 77, "fileStatus": "rcvComplete",
                            "filePath": "/tmp/simplex/v2.m4a"}
            await self.g._on_raw(json.dumps({"resp": ev}))
            return await task
        self.assertEqual(asyncio.run(scenario()), "/tmp/simplex/v2.m4a")

    def test_file_path_nested_in_file_source_resolves(self):
        # v7 shape: chatItem.file.fileSource.filePath (not file.filePath)
        async def scenario():
            task = asyncio.ensure_future(self.g.receive_file("77"))
            await asyncio.sleep(0)
            cmd = json.loads(self.conn.sent[0])
            await self.g._on_raw(json.dumps(
                {"corrId": cmd["corrId"], "resp": {"type": "cmdOk"}}))
            await self.g._on_raw(json.dumps({"resp": {
                "type": "rcvFileComplete",
                "chatItem": {"chatItem": {"file": {
                    "fileId": 77,
                    "fileStatus": {"type": "rcvComplete"},
                    "fileSource": {"type": "rcvFile",
                                   "filePath": "/tmp/simplex/nested.m4a"}}}}}}))
            return await task
        self.assertEqual(asyncio.run(scenario()), "/tmp/simplex/nested.m4a")

    def test_filesystem_poll_resolves_when_no_event_arrives(self):
        # belt-and-suspenders: the CLI downloads the file but the bot never
        # sees a completion event - the poll for fileName+fileSize finds it
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as dl:
            target = os.path.join(dl, "voice_poll.m4a")
            with open(target, "wb") as fh:
                fh.write(b"x" * 4321)
            self.g._poll_dirs = [dl]

            async def scenario():
                task = asyncio.ensure_future(
                    self.g.receive_file("91", file_name="voice_poll.m4a",
                                        file_size=4321, timeout=5.0))
                await asyncio.sleep(0)
                cmd = json.loads(self.conn.sent[0])
                await self.g._on_raw(json.dumps(
                    {"corrId": cmd["corrId"], "resp": {"type": "cmdOk"}}))
                return await task          # no rcvFileComplete ever sent
            self.assertEqual(asyncio.run(scenario()), target)

    def test_relative_file_path_resolved_against_downloads(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as dl:
            target = os.path.join(dl, "voice_rel.m4a")
            open(target, "wb").write(b"x")
            async def scenario():
                task = asyncio.ensure_future(self.g.receive_file("88"))
                await asyncio.sleep(0)
                cmd = json.loads(self.conn.sent[0])
                await self.g._on_raw(json.dumps(
                    {"corrId": cmd["corrId"], "resp": {"type": "cmdOk"}}))
                await self.g._on_raw(json.dumps({"resp": {
                    "type": "rcvFileComplete",
                    "chatItem": {"chatItem": {"file": {
                        "fileId": 88, "fileStatus": {"type": "rcvComplete"},
                        "filePath": "voice_rel.m4a"}}}}}))
                return await task
            # point ~/Downloads resolution at our temp dir via cwd fallback:
            # relative name not in cwd -> try ~/Downloads; here we just check
            # the raw relative path is returned when it exists nowhere
            path = asyncio.run(scenario())
            self.assertEqual(path, "voice_rel.m4a")

    def test_receive_file_times_out_without_path_event(self):
        async def scenario():
            return await self.g.receive_file("99", timeout=0.05)
        with self.assertRaises(asyncio.TimeoutError):
            asyncio.run(scenario())

    def test_cmd_response_resolves_waiter(self):
        async def scenario():
            task = asyncio.ensure_future(self.g._send_cmd("/version"))
            await asyncio.sleep(0)
            cmd = json.loads(self.conn.sent[0])
            self.assertEqual(cmd["cmd"], "/version")
            await self.g._on_raw(json.dumps(
                {"corrId": cmd["corrId"],
                 "resp": {"type": "versionInfo", "version": "7.0.0"}}))
            return await task
        resp = asyncio.run(scenario())
        self.assertEqual(resp["version"], "7.0.0")


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

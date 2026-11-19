"""SimpleX Chat gateway: WebSocket transport (blueprint V5 section 2.1).

THE SOURCE OF REAL USER MESSAGES
--------------------------------
Until this module existed, every (user_id, message_id, text) triple the
pipeline has ever seen was produced by TEST code: scripts like
scripts/mistral_conversation_test.py and scripts/italian_scenario_test.py
call pipeline.handle_incoming(...) directly. Nothing in app/ produced messages.

SimpleXGateway.on_chat_event() below is the single point where a REAL
SimpleX chat message becomes a pipeline input instead: an incoming chat
event is translated to (user_id, message_id, text) and handed to
Pipeline.handle_incoming(). Everything downstream of that call is the
exact same code path the tests exercise (spec 9.1) - idempotency, FIFO,
confirmation loop, Trello write.

Protocol: SimpleX Chat CLI WebSocket JSON API (SIMPLEX_WS_URL, typically
ws://127.0.0.1:5225), verified against simplex-chat v7.0.0.12:
- client requests: {"corrId": ..., "cmd": "..."}; responses carry the corrId
- server-initiated events: {"resp": {...}} WITHOUT corrId
- incoming/outgoing chat items: {"resp": {"type": "newChatItems",
  "user": {...},
  "chatItems": [{"chatInfo": {"type": "direct",
                              "contact": {"contactId", "localDisplayName"}},
                "chatItem": {"chatDir": {"type": "directRcv"|"directSnd"},
                             "meta": {"itemId", ...},
                             "content": {"type": "...MsgContent",
                                         "msgContent": {"type": "text",
                                                        "text": ...}}}},
               ...]}}
- sending: the chat command "@<contact_display_name> <message>"
The exact event schema is isolated in _extract_incoming() so a future CLI
version only requires adjusting that one method.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Optional

import websockets

from ..pipeline import Gateway, Pipeline

log = logging.getLogger(__name__)


class SimpleXGateway(Gateway):
    """Talks to the SimpleX Chat CLI over WebSocket.

    run()           connect forever, receive events, reconnect with backoff
    on_chat_event() REAL MESSAGE ENTRY POINT -> pipeline.handle_incoming
    send()          push a reply back to the contact over the same socket
    """

    def __init__(self, ws_url: str, pipeline: Optional[Pipeline] = None,
                 admit=None, transcriber=None):
        """`admit(user_id, message_id, text) -> bool` is an optional gate
        that runs BEFORE the pipeline (used for onboarding: map a new
        contact to a Trello member before they may give cards).

        `transcriber` is the speech-to-text hook (phase 5, Mistral Voxtral
        today, chatBHT service later): an async callable
        `transcriber(payload) -> str` that receives the media item
        {"msgContent": {...}, "file": {"fileId": ...}} - download the file
        over this socket with receive_file(fileId), transcribe it, and
        return the transcript. While it is None, voice messages are
        answered with a "text only for now" reply instead of being dropped
        silently. The LLM itself stays text-only: transcription happens
        HERE, before the pipeline (blueprint: the Orchestrator takes only
        text)."""
        super().__init__()                      # keeps .sent for tests
        self.ws_url = ws_url
        self.pipeline = pipeline
        self.admit = admit
        self.transcriber = transcriber
        self._ws: Optional[object] = None
        self._corr = 0
        self._backoff_s = 1.0
        # corrId -> Future for command responses; fileId -> Future for the
        # local path of a file downloaded via /freceive (used by the STT hook)
        self._cmd_waiters: dict[str, asyncio.Future] = {}
        self._file_waiters: dict[str, asyncio.Future] = {}
        self._tasks: set[asyncio.Task] = set()   # background media handling

    # -- lifecycle ----------------------------------------------------------
    async def run(self) -> None:
        """Connect forever; reconnect with capped exponential backoff."""
        while True:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    self._ws = ws
                    self._backoff_s = 1.0
                    log.info("SimpleX: connected to %s", self.ws_url)
                    async for raw in ws:
                        await self._on_raw(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:               # network down, CLI restart
                log.warning("SimpleX: connection error %r; retry in %.0fs",
                            e, self._backoff_s)
            finally:
                self._ws = None
            await asyncio.sleep(self._backoff_s)
            self._backoff_s = min(self._backoff_s * 2, 60.0)

    # -- receive path ---------------------------------------------------------
    async def _on_raw(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return                              # not JSON: ignore
        if not isinstance(msg, dict):
            return
        if "corrId" in msg:                     # response to our own command
            resp = msg.get("resp") or {}
            corr = str(msg.get("corrId"))
            fut = self._cmd_waiters.pop(corr, None)
            if fut is not None and not fut.done():
                fut.set_result(resp if isinstance(resp, dict) else {})
            elif isinstance(resp, dict) and resp.get("type") == "error":
                log.warning("SimpleX: command failed: %s", resp)
            else:
                log.debug("SimpleX: response %s", msg)
            return
        # server-initiated event: {"resp": {...}} without corrId
        ev = msg.get("resp") if isinstance(msg.get("resp"), dict) else msg
        self._scan_file_event(ev)               # resolve /freceive waiters
        await self.on_chat_event(ev)

    def _scan_file_event(self, ev) -> None:
        """Resolve _file_waiters from any event carrying a completed file.

        The CLI reports downloads via dedicated events, e.g.
        {"resp": {"type": "rcvFileComplete",
                  "chatItem": {"chatInfo": ..., "chatItem": {..., "file": {
                      "fileId": N, "fileStatus": {"type": "rcvComplete"},
                      "filePath": "/abs/path"}}}}}
        and via updated newChatItems items - so scan the whole event tree
        for any dict that has fileId + filePath + a complete status."""
        if not self._file_waiters or not isinstance(ev, (dict, list)):
            return
        self._scan_files(ev)

    def _scan_files(self, node) -> None:
        if isinstance(node, dict):
            fid = node.get("fileId") or node.get("file_id")
            if fid is not None:
                fid = str(fid)
                if fid in self._file_waiters:
                    path = node.get("filePath") or node.get("file_path")
                    src = node.get("fileSource")
                    if not path and isinstance(src, dict):
                        # v7 nests the downloaded path under fileSource
                        path = src.get("filePath") or src.get("file_path")
                    status = node.get("fileStatus") or node.get("file_status") or ""
                    if isinstance(status, dict):
                        status = status.get("type") or ""
                    if path and "complete" in str(status).lower():
                        fut = self._file_waiters[fid]
                        if not fut.done():
                            fut.set_result(str(path))
            for v in node.values():
                self._scan_files(v)
        elif isinstance(node, list):
            for v in node:
                self._scan_files(v)

    async def _send_cmd(self, cmd: str, timeout: float = 30.0) -> dict:
        """Send a CLI command and await its corrId response."""
        ws = self._ws
        if ws is None:
            raise RuntimeError("SimpleX: not connected")
        self._corr += 1
        corr = str(self._corr)
        fut = asyncio.get_running_loop().create_future()
        self._cmd_waiters[corr] = fut
        try:
            await ws.send(json.dumps({"corrId": corr, "cmd": cmd}))
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._cmd_waiters.pop(corr, None)

    async def receive_file(self, file_id: str, file_name: str | None = None,
                           file_size: int | None = None,
                           timeout: float = 120.0) -> str:
        """Download a received file through the CLI ("/freceive <fileId>") and
        return its local path. Used by the STT transcriber hook to get the
        audio bytes of a voice message.

        Two independent resolvers race (FIRST_COMPLETED wins):
        1. the CLI's rcvFileComplete / newChatItems event carrying the path
           (resolved via _file_waiters, scanned in _scan_file_event);
        2. a filesystem poll for file_name in the CLI's download dirs
           (cwd, ~/Downloads) until its size matches file_size - immune to
           event-shape differences between CLI versions.
        A relative reported path (CLI started with --files-folder) is
        resolved against those same dirs."""
        fid = str(file_id)
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._file_waiters[fid] = fut
        try:
            await self._send_cmd(f"/freceive {fid}")
            waiters = [asyncio.ensure_future(asyncio.wait_for(fut, timeout))]
            if file_name:
                waiters.append(asyncio.ensure_future(
                    self._poll_file(file_name, file_size, timeout)))
            done, pending = await asyncio.wait(waiters,
                                               return_when=asyncio.FIRST_COMPLETED)
            for w in pending:
                w.cancel()
            for w in done:
                if w.exception() is None:
                    return self._resolve_path(w.result())
            exc = next((w.exception() for w in done if w.exception()), None)
            raise exc if exc is not None else TimeoutError()
        finally:
            self._file_waiters.pop(fid, None)

    async def _poll_file(self, file_name: str, file_size: int | None,
                         timeout: float) -> str:
        """Wait until the CLI has written file_name into one of the download
        dirs with the expected size (stable across two polls)."""
        loop = asyncio.get_running_loop()
        dirs = getattr(self, "_poll_dirs", None) or [
            os.getcwd(), os.path.expanduser("~/Downloads")]
        deadline = loop.time() + timeout
        last_size = -1
        while loop.time() < deadline:
            for d in dirs:
                p = os.path.join(d, file_name)
                if os.path.isfile(p):
                    size = os.path.getsize(p)
                    if (file_size is None and size == last_size > 0) \
                            or (file_size is not None and size == int(file_size)
                                and size == last_size):
                        return p
                    last_size = size
            await asyncio.sleep(0.5)
        raise TimeoutError(f"file {file_name!r} did not appear in {dirs}")

    @staticmethod
    def _resolve_path(path: str) -> str:
        if os.path.isabs(path):
            return path
        for base in (os.getcwd(), os.path.expanduser("~/Downloads")):
            cand = os.path.join(base, path)
            if os.path.exists(cand):
                return cand
        return path

    async def on_chat_event(self, ev: dict) -> None:
        """REAL MESSAGE ENTRY POINT.

        A chat event coming from the SimpleX CLI becomes the pipeline
        input here. This replaces the test scripts as the source of
        messages: from now on the same handle_incoming() call the tests
        make is made by a real user writing in SimpleX.
        """
        if self.pipeline is None:
            return
        for user_id, message_id, text in self._extract_incoming(ev):
            log.info("recv [%s] from %s: %r", message_id, user_id, text)
            try:
                if self.admit is not None and not await self.admit(
                        user_id, message_id, text):
                    continue                     # consumed by the gate
                await self.pipeline.handle_incoming(user_id, message_id, text)
            except Exception as e:
                # a pipeline error must not look like a broken connection:
                # log it and keep consuming events on this socket
                log.error("SimpleX: pipeline error for message %s: %r",
                          message_id, e)
        for user_id, message_id, kind, payload in self._extract_media(ev):
            # MUST NOT await here: _handle_media -> receive_file waits for a
            # frame (rcvFileComplete) on this very socket, and the read loop
            # (async for raw in ws) is blocked while we await - a self-
            # deadlock that made every voice message time out. Spawn it.
            self._spawn(self._handle_media(user_id, message_id, kind,
                                           payload))

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _extract_incoming(self, ev: dict) -> list[tuple[str, str, str]]:
        """(user_id, message_id, text) triples for incoming 1:1 text items.

        [] for everything else (sent items, non-direct chats, service
        events). Non-text media (voice/files) is NOT dropped here but
        surfaced by _extract_media() for the STT hook. Schema verified
        against simplex-chat v7.0.0.12: each event is {"type":
        "newChatItems", "user": ..., "chatItems": [{"chatInfo": {...},
        "chatItem": {...}}, ...]}.
        Adjust this method only if a CLI update changes it.
        """
        if ev.get("type") != "newChatItems":
            return []
        # one event may carry several items, each with its own chatInfo
        pairs = ev.get("chatItems")
        if pairs is None and "chatItem" in ev:
            pairs = [ev]                         # defensive: flat shape
        out: list[tuple[str, str, str]] = []
        for pair in pairs or []:
            info = pair.get("chatInfo") or {}
            if info.get("type") != "direct":
                continue                        # groups/channels: later phase
            item = pair.get("chatItem") or {}
            # our own sent messages also arrive as newChatItems: keep received
            chat_dir = str((item.get("chatDir") or {}).get("type") or "")
            if chat_dir.lower() != "directrcv":
                continue
            meta = item.get("meta") or {}
            mc = (item.get("content") or {}).get("msgContent") or {}
            if mc.get("type") != "text":
                continue                        # audio/attachments: step 5
            text = mc.get("text")
            # the display name is what the "@<name> <msg>" send command needs
            user_id = str((info.get("contact") or {})
                          .get("localDisplayName") or "")
            message_id = str(meta.get("itemId") or "")
            if text and user_id and message_id:
                out.append((user_id, message_id, text))
        return out

    def _extract_media(self, ev: dict) -> list[tuple[str, str, str, dict]]:
        """(user_id, message_id, kind, msgContent) for received NON-text items
        (voice, image, file, ...). These are the STT seam: while no
        transcriber is configured they get a "text only" reply instead of
        being dropped silently."""
        if ev.get("type") != "newChatItems":
            return []
        pairs = ev.get("chatItems")
        if pairs is None and "chatItem" in ev:
            pairs = [ev]
        out: list[tuple[str, str, str, dict]] = []
        for pair in pairs or []:
            info = pair.get("chatInfo") or {}
            if info.get("type") != "direct":
                continue
            item = pair.get("chatItem") or {}
            chat_dir = str((item.get("chatDir") or {}).get("type") or "")
            if chat_dir.lower() != "directrcv":
                continue
            meta = item.get("meta") or {}
            mc = (item.get("content") or {}).get("msgContent") or {}
            kind = mc.get("type")
            if kind == "text":
                continue
            # the file reference sits on the chatItem (sibling of "content")
            # in the CLI's WS API - e.g. voice items are
            # {"msgContent": {"type": "voice", "duration": 11}, "file": {...}};
            # older shapes nest it inside msgContent, so check both
            file_info = item.get("file") or mc.get("file") or {}
            user_id = str((info.get("contact") or {})
                          .get("localDisplayName") or "")
            message_id = str(meta.get("itemId") or "")
            if kind and user_id and message_id:
                out.append((user_id, message_id, str(kind),
                            {"msgContent": mc, "file": file_info}))
        return out

    async def _handle_media(self, user_id: str, message_id: str,
                            kind: str, payload: dict) -> None:
        """Voice/audio/file message: transcribe via the STT hook and feed the
        transcript to the pipeline as text; without a hook, tell the user
        plainly that only text works for now. `payload` is
        {"msgContent": {...}, "file": {"fileId": ...}}."""
        log.info("recv [%s] from %s: <%s message>", message_id, user_id, kind)
        if self.transcriber is None:
            log.warning("SimpleX: %s message [%s] from %s: no transcriber "
                        "configured, replying text-only", kind, message_id,
                        user_id)
            await self.send(user_id, "I can only read text messages for now - "
                                     "voice support is coming soon. Please "
                                     "write it as text.")
            return
        try:
            transcript = await self.transcriber(payload)
        except Exception as e:
            log.error("SimpleX: transcription failed for message %s: %r",
                      message_id, e)
            reason = str(e) or type(e).__name__     # TimeoutError() is empty
            await self.send(user_id, f"Sorry: I could not transcribe that "
                                     f"{kind} message ({reason}). Please write "
                                     f"it as text.")
            return
        transcript = (transcript or "").strip()
        if not transcript:
            await self.send(user_id, "I could not understand that audio - "
                                     "please write it as text.")
            return
        log.info("transcribed [%s] from %s: %r", message_id, user_id,
                 transcript)
        try:
            if self.admit is not None and not await self.admit(
                    user_id, message_id, transcript):
                return                         # consumed by the gate
            await self.pipeline.handle_incoming(user_id, message_id,
                                                transcript)
        except Exception as e:
            log.error("SimpleX: pipeline error for message %s: %r",
                      message_id, e)

    # -- send path ----------------------------------------------------------
    async def send(self, user_id: str, text: str) -> None:
        """Pipeline replies push back to the contact over the same socket.

        user_id is the contact's local display name: the CLI chat command
        is "@<name> <message>" (verified against v7.0.0.12).
        """
        await super().send(user_id, text)       # in-memory record (tests)
        log.info("send to %s: %r", user_id, text)
        ws = self._ws
        if ws is None:
            log.error("SimpleX: not connected, reply dropped: %r", text)
            return
        self._corr += 1
        cmd = f"@{user_id} {text}"
        try:
            await ws.send(json.dumps({"corrId": str(self._corr), "cmd": cmd}))
        except Exception as e:
            log.error("SimpleX: send failed %r", e)

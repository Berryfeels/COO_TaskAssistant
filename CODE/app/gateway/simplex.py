"""SimpleX Chat gateway: WebSocket transport (blueprint V5 section 2.1).

THE SOURCE OF REAL USER MESSAGES
--------------------------------
Until this module existed, every (user_id, message_id, text) triple the
pipeline has ever seen was produced by TEST code: scripts like
scripts/mistral_conversation_test.py and scripts/italian_scenario_test.py
call pipeline.handle_incoming(...) directly, and the unit tests drive the
same method through tests/base.py. Nothing in app/ produced messages.

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
                 admit=None):
        """`admit(user_id, message_id, text) -> bool` is an optional gate
        that runs BEFORE the pipeline (used for onboarding: map a new
        contact to a Trello member before they may give tasks)."""
        super().__init__()                      # keeps .sent for tests
        self.ws_url = ws_url
        self.pipeline = pipeline
        self.admit = admit
        self._ws: Optional[object] = None
        self._corr = 0
        self._backoff_s = 1.0

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
            if isinstance(resp, dict) and resp.get("type") == "error":
                log.warning("SimpleX: command failed: %s", resp)
            else:
                log.debug("SimpleX: response %s", msg)
            return
        # server-initiated event: {"resp": {...}} without corrId
        ev = msg.get("resp") if isinstance(msg.get("resp"), dict) else msg
        await self.on_chat_event(ev)

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

    def _extract_incoming(self, ev: dict) -> list[tuple[str, str, str]]:
        """(user_id, message_id, text) triples for incoming 1:1 text items.

        [] for everything else (sent items, non-direct chats, audio,
        files, service events). Schema verified against simplex-chat
        v7.0.0.12: each event is {"type": "newChatItems", "user": ...,
        "chatItems": [{"chatInfo": {...}, "chatItem": {...}}, ...]}.
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

"""Core pipeline: FIFO ingestion, interpretation, immediate writes.

Implements spec 9.1 with the agreed semantics:
- FIFO per user applies to ingestion only; every message is an independent
  process (no blocking, no superseding).
- Since 2026-10-29 there is no confirmation loop: write actions are applied
  immediately and the bot only confirms the outcome. The confirmation
  matching below remains only to drain pendings created before the change.
- Trello is the single source of truth: every Trello write happens FIRST and
  the local DB (pending status, processed IDs) is updated only AFTER the
  write succeeded. Queries read Trello only, never the local DB.
- mark_processed(msg.id) means "this message has been handled"; it never
  implies the action was executed (spec 5.2).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .config import Settings
from .confirm.pending import PendingService
from .notify import builder
from .orchestrator.agent import LLMProvider
from .schema import Action, Clarification, Card
from .similarity import is_similar
from .storage.db import DB
from .trello.client import TrelloLike


@dataclass
class Gateway:
    """Outbound message sink. The real SimpleX gateway (phase 3) implements
    send(); tests capture messages here."""
    sent: list[tuple[str, str]] = field(default_factory=list)  # (user_id, text)

    async def send(self, user_id: str, text: str) -> None:
        self.sent.append((user_id, text))


class Pipeline:
    def __init__(self, db: DB, trello: TrelloLike, llm: LLMProvider,
                 pending: PendingService, gateway: Gateway,
                 settings: Settings):
        self.db, self.trello, self.llm = db, trello, llm
        self.pending, self.gateway, self.settings = pending, gateway, settings

    # -- ingestion (FIFO) ------------------------------------------------------
    async def handle_incoming(self, user_id: str, message_id: str, text: str):
        """Entry point: push to the FIFO, then drain in arrival order."""
        self.db.queue_push(user_id, message_id, text)
        await self.drain(user_id)

    async def drain(self, user_id: str) -> None:
        while True:
            item = self.db.queue_pop(user_id)
            if item is None:
                return
            msg_id, text = item
            await self._process(user_id, msg_id, text)

    # -- one message = one independent process (spec 5.3) -----------------------
    async def _process(self, user_id: str, message_id: str, text: str) -> None:
        if self.db.already_processed(message_id):      # idempotency (5.2)
            return

        # 1. Is this a confirmation of an existing pending action?
        #    Trello is the source of truth: write there FIRST, update the DB
        #    only after the write succeeded (spec: the DB follows Trello).
        decision = self.pending.match_confirmation(user_id, text)
        if decision.kind == "confirm":
            try:
                result = self.trello.apply(decision.card)   # the only Trello write
            except Exception as e:
                # the pending stays active, so a later "OK <id>" retries the
                # write; the message itself is handled (not replayed)
                self.db.mark_processed(message_id)
                await self.gateway.send(
                    user_id, builder.render_write_error(decision.card, e))
                return
            self._record_list(decision.card, result)        # DB AFTER Trello
            self.pending.apply_by_id(decision.pending_id)   # DB AFTER Trello
            self.db.mark_processed(message_id)
            await self.gateway.send(user_id, builder.render_result(decision.card, result))
            return
        if decision.kind == "ambiguous":
            self.db.mark_processed(message_id)
            await self.gateway.send(user_id, builder.render_ambiguous(decision.active))
            return

        # 2. The LLM owns the conversation: interpret with per-user
        #    history (greetings/chat are answered freely by the model).
        try:
            card = self.llm.interpret(
                text, {"source_message_id": message_id, "user_id": user_id},
                self.trello)
        except Clarification as c:
            self.db.mark_processed(message_id)          # question sent, no pending
            await self.gateway.send(user_id, builder.render_clarification(c.question))
            return
        except Exception as e:
            # API/network failure: without this the user gets silence
            # (the gateway catches the exception and only logs it)
            self.db.mark_processed(message_id)
            await self.gateway.send(user_id, builder.render_error(e))
            return

        # 3. Free conversation: answer directly, nothing happens on the board.
        if card.action == Action.chat:
            self.db.mark_processed(message_id)
            await self.gateway.send(user_id, builder.render_chat(card))
            return

        # 4. Queries are read-only: answer immediately.
        if card.action == Action.query:
            await self._answer_query(user_id, card)
            self.db.mark_processed(message_id)  # after the reply: a crash
            return                             # keeps the message retryable

        # 5. Write action: validate, apply immediately, confirm the
        #    outcome. No confirmation loop (spec change 2026-10-29): the
        #    action lands on the board right away and the bot only reports.
        try:
            card.validate()
        except ValueError as e:
            # an action with no real change (e.g. move_date without a new
            # date) would be applied as a Trello no-op and reported as done:
            # reject it here and tell the user instead of crashing silently
            self.db.mark_processed(message_id)
            await self.gateway.send(user_id, builder.render_invalid(card, e))
            return
        similar = self._similar_cards(user_id, card)
        try:
            result = self.trello.apply(card)          # Trello FIRST (source of truth)
        except Exception as e:
            self.db.mark_processed(message_id)        # message handled, nothing written
            await self.gateway.send(
                user_id, builder.render_write_error(card, e))
            return
        self._record_list(card, result)               # DB AFTER the Trello write
        self.db.mark_processed(message_id)            # DB AFTER the Trello write
        await self.gateway.send(
            user_id, builder.render_result(card, result, similar))

    def _record_list(self, card: Card, result: dict) -> None:
        """Mirror a created column into the local DB. Trello is the source of
        truth, so this runs only after create_list succeeded."""
        if card.action == Action.add_list and result.get("id"):
            self.db.list_save(result["id"], card.title, time.time())

    def _similar_cards(self, user_id: str, card: Card) -> list[str]:
        """Near-duplicate guard: message-ID idempotency (spec 5.2) cannot
        catch a reworded repeat of a forgotten card, so warn about active
        pendings and open cards with a similar title. Only a heads-up in
        the confirmation - the user decides; nothing is ever blocked."""
        if card.action != Action.add_card:
            return []
        hits: list[str] = []
        for row in self.pending.active(user_id):
            if (row.card.action == Action.add_card and row.card.title
                    and is_similar(row.card.title, card.title)):
                hits.append(f"'{row.card.title}' (pending [{row.pending_id}])")
        for existing in self.trello.board_cards():
            if existing.get("closed") or not existing.get("name"):
                continue
            if is_similar(existing["name"], card.title):
                hits.append(f"'{existing['name']}' (already on the board)")
        return hits

    async def _answer_query(self, user_id: str, card: Card) -> None:
        # Reads TRELLO only, never the local DB: Trello is the single source
        # of truth and users look at the board, not at our SQLite. Each line
        # shows the column and the done flag exactly as the board shows them.
        # board_cards(), not search_cards(""): Trello's /1/search rejects an
        # empty query with HTTP 400 and its index lags behind anyway
        cards = self.trello.board_cards() or []
        columns = {l["id"]: l["name"] for l in self.trello.list_lists()}
        open_cards = [c for c in cards if not c.get("closed")]
        if not open_cards:
            await self.gateway.send(user_id, "You have no open cards.")
            return
        lines = []
        for c in open_cards:
            column = columns.get(c.get("idList"), "?")
            done = " (done)" if c.get("dueComplete") else ""
            due = f", due {c['due']}" if c.get("due") else ""
            lines.append(f"- {c['name']} [{column}]{done}{due}")
        await self.gateway.send(user_id,
                                "Your open cards (from Trello):\n"
                                + "\n".join(lines))

    # -- scheduler jobs (spec 5.1.3, 5.1.4) --------------------------------------
    async def reminder_job(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        for row in self.pending.due_for_reminder(now):
            if self.pending.quiet_hours():
                continue                                # deferred to 05:00+
            self.pending.bump_reminder(row.pending_id, now)
            hours_left = max(
                1, int((row.created_at + self.settings.pending_ttl_hours * 3600
                        - now) / 3600))
            await self.gateway.send(row.user_id,
                                    builder.render_reminder(row.pending_id,
                                                            row.card, hours_left))

    async def expiry_job(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        for pending_id, user_id, card in self.pending.expire(now):
            await self.gateway.send(user_id,
                                    builder.render_cancelled(pending_id, card))

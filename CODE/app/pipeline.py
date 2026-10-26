"""Core pipeline: FIFO ingestion, interpretation, confirmation loop, writes.

Implements spec 9.1 with the agreed semantics:
- FIFO per user applies to ingestion only; every message is an independent
  pending process (no blocking, no superseding).
- mark_processed(msg.id) means "this message has been handled"; it is called
  in the same transaction as pending_save / after sending a clarification,
  and again after a confirmation is applied. It never implies the action
  was executed (spec 5.2).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .config import Settings
from .confirm.pending import PendingDecision, PendingService
from .notify import builder
from .orchestrator.agent import LLMProvider
from .schema import Action, Clarification, Task
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
        decision = self.pending.match_confirmation(user_id, text)
        if decision.kind == "applied":
            self.db.mark_processed(message_id)          # message fully handled
            try:
                result = self.trello.apply(decision.task)   # the only Trello write
            except Exception as e:
                # the pending is consumed: tell the user instead of
                # failing silently (the error must never look like a
                # connection problem)
                await self.gateway.send(
                    user_id, builder.render_write_error(decision.task, e))
                return
            await self.gateway.send(user_id, builder.render_result(decision.task, result))
            return
        if decision.kind == "ambiguous":
            self.db.mark_processed(message_id)
            await self.gateway.send(user_id, builder.render_ambiguous(decision.active))
            return

        # 2. New instruction: interpret (LLM + Trello read tools).
        try:
            task = self.llm.interpret(text, {"source_message_id": message_id},
                                      self.trello)
        except Clarification as c:
            self.db.mark_processed(message_id)          # question sent, no pending
            await self.gateway.send(user_id, builder.render_clarification(c.question))
            return

        # 3. Queries are read-only: answer immediately, no pending.
        if task.action == Action.query:
            self.db.mark_processed(message_id)
            await self._answer_query(user_id, task)
            return

        # 4. Write action: validate, save pending, mark processed (same
        #    transaction semantics, spec 5.2), ask for confirmation.
        task.validate()
        similar = self._similar_tasks(user_id, task)
        pending_id = self.pending.save(user_id, task, time.time())
        self.db.mark_processed(message_id)              # NOT an execution
        await self.gateway.send(
            user_id, builder.render_confirmation(pending_id, task, similar))

    def _similar_tasks(self, user_id: str, task: Task) -> list[str]:
        """Near-duplicate guard: message-ID idempotency (spec 5.2) cannot
        catch a reworded repeat of a forgotten task, so warn about active
        pendings and open cards with a similar title. Only a heads-up in
        the confirmation - the user decides; nothing is ever blocked."""
        if task.action != Action.create:
            return []
        hits: list[str] = []
        for row in self.pending.active(user_id):
            if (row.task.action == Action.create and row.task.title
                    and is_similar(row.task.title, task.title)):
                hits.append(f"'{row.task.title}' (pending [{row.pending_id}])")
        for card in self.trello.board_cards():
            if card.get("closed") or not card.get("name"):
                continue
            if is_similar(card["name"], task.title):
                hits.append(f"'{card['name']}' (already on the board)")
        return hits

    async def _answer_query(self, user_id: str, task: Task) -> None:
        cards = self.trello.search_cards("") or []
        open_cards = [c for c in cards if not c.get("closed")]
        if not open_cards:
            await self.gateway.send(user_id, "You have no open tasks.")
            return
        lines = [f"- {c['name']}" + (f" (due {c['due']})" if c.get("due") else "")
                 for c in open_cards]
        await self.gateway.send(user_id, "Your open tasks:\n" + "\n".join(lines))

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
                                                            row.task, hours_left))

    async def expiry_job(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        for pending_id, user_id, task in self.pending.expire(now):
            await self.gateway.send(user_id,
                                    builder.render_cancelled(pending_id, task))

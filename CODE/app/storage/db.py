"""SQLite storage: users, processed message IDs, pending actions, FIFO queue.

Zero external dependencies (sqlite3 stdlib). Spec sections 5.1-5.3 and 9.1.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from ..schema import Action, Task

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    simplex_id TEXT PRIMARY KEY,
    trello_member_id TEXT,
    role TEXT DEFAULT 'worker'
);
CREATE TABLE IF NOT EXISTS processed (
    message_id TEXT PRIMARY KEY,
    processed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pending (
    pending_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    task_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    reminders_sent INTEGER NOT NULL DEFAULT 0,
    last_reminder_at REAL,
    status TEXT NOT NULL DEFAULT 'pending'   -- pending | applied | cancelled | superseded
);
CREATE TABLE IF NOT EXISTS queue (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_queue_user ON queue(user_id, seq);
"""


@dataclass
class PendingRow:
    pending_id: str
    user_id: str
    task: Task
    created_at: float
    reminders_sent: int
    last_reminder_at: Optional[float]
    status: str


class DB:
    def __init__(self, path: str = ":memory:"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # -- users ------------------------------------------------------------
    def upsert_user(self, simplex_id: str, trello_member_id: str,
                    role: str = "worker") -> None:
        self.conn.execute(
            "INSERT INTO users (simplex_id, trello_member_id, role) VALUES (?,?,?) "
            "ON CONFLICT(simplex_id) DO UPDATE SET trello_member_id=excluded.trello_member_id, role=excluded.role",
            (simplex_id, trello_member_id, role))
        self.conn.commit()

    def user(self, simplex_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM users WHERE simplex_id = ?", (simplex_id,)).fetchone()

    # -- processed (idempotency, spec 5.2) --------------------------------
    def already_processed(self, message_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM processed WHERE message_id = ?", (message_id,)
        ).fetchone() is not None

    def mark_processed(self, message_id: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO processed (message_id, processed_at) VALUES (?,?)",
            (message_id, time.time()))
        self.conn.commit()

    # -- pending (spec 5.1) ------------------------------------------------
    def pending_save(self, user_id: str, task: Task,
                     created_at: float) -> str:
        pending_id = "P" + uuid.uuid4().hex[:6].upper()
        self.conn.execute(
            "INSERT INTO pending (pending_id, user_id, task_json, created_at) VALUES (?,?,?,?)",
            (pending_id, user_id, json.dumps(task.__dict__), created_at))
        self.conn.commit()
        return pending_id

    def pending_get(self, pending_id: str) -> Optional[PendingRow]:
        row = self.conn.execute(
            "SELECT * FROM pending WHERE pending_id = ? AND status = 'pending'",
            (pending_id,)).fetchone()
        return self._row_to_pending(row) if row else None

    def pending_set_status(self, pending_id: str, status: str) -> Optional[Task]:
        row = self.conn.execute(
            "SELECT * FROM pending WHERE pending_id = ?", (pending_id,)).fetchone()
        if not row:
            return None
        self.conn.execute(
            "UPDATE pending SET status = ? WHERE pending_id = ?",
            (status, pending_id))
        self.conn.commit()
        task = Task(**json.loads(row["task_json"]))
        task.action = Action(task.action)
        return task

    def pending_active(self, user_id: str) -> list[PendingRow]:
        rows = self.conn.execute(
            "SELECT * FROM pending WHERE user_id = ? AND status = 'pending' ORDER BY created_at",
            (user_id,)).fetchall()
        return [self._row_to_pending(r) for r in rows]

    def pending_bump_reminder(self, pending_id: str, at: float) -> None:
        self.conn.execute(
            "UPDATE pending SET reminders_sent = reminders_sent + 1, last_reminder_at = ? "
            "WHERE pending_id = ?", (at, pending_id))
        self.conn.commit()

    def pending_due_reminders(self, now: float, interval_h: int,
                              max_reminders: int) -> list[PendingRow]:
        rows = self.conn.execute(
            "SELECT * FROM pending WHERE status = 'pending' AND reminders_sent < ? "
            "AND (last_reminder_at IS NULL OR last_reminder_at <= ?)",
            (max_reminders, now - interval_h * 3600)).fetchall()
        return [self._row_to_pending(r) for r in rows]

    def pending_expired(self, now: float, ttl_h: int) -> list[PendingRow]:
        rows = self.conn.execute(
            "SELECT * FROM pending WHERE status = 'pending' AND created_at <= ?",
            (now - ttl_h * 3600,)).fetchall()
        return [self._row_to_pending(r) for r in rows]

    @staticmethod
    def _row_to_pending(row: sqlite3.Row) -> PendingRow:
        task = Task(**json.loads(row["task_json"]))
        task.action = Action(task.action)
        return PendingRow(
            pending_id=row["pending_id"], user_id=row["user_id"],
            task=task,
            created_at=row["created_at"], reminders_sent=row["reminders_sent"],
            last_reminder_at=row["last_reminder_at"], status=row["status"])

    # -- FIFO queue (spec 5.3: ingestion only) -----------------------------
    def queue_push(self, user_id: str, message_id: str, payload: str) -> None:
        self.conn.execute(
            "INSERT INTO queue (user_id, message_id, payload) VALUES (?,?,?)",
            (user_id, message_id, payload))
        self.conn.commit()

    def queue_pop(self, user_id: str) -> Optional[tuple[str, str]]:
        row = self.conn.execute(
            "SELECT seq, message_id, payload FROM queue WHERE user_id = ? "
            "ORDER BY seq LIMIT 1", (user_id,)).fetchone()
        if not row:
            return None
        self.conn.execute("DELETE FROM queue WHERE seq = ?", (row["seq"],))
        self.conn.commit()
        return row["message_id"], row["payload"]

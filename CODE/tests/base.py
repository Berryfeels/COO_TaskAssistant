"""Shared fixtures: a wired pipeline with FakeTrello, MockLLM, in-memory DB."""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from app.confirm.pending import PendingService
from app.orchestrator.agent import MockLLM
from app.pipeline import Gateway, Pipeline
from app.storage.db import DB
from app.trello.client import FakeTrello


def make_pipeline(settings: Settings = None):
    db = DB(":memory:")
    trello = FakeTrello()
    settings = settings or Settings()
    pending = PendingService(db, settings)
    gateway = Gateway()
    pipe = Pipeline(db, trello, MockLLM(), pending, gateway, settings)
    return pipe, db, trello, gateway, pending


def run(coro):
    return asyncio.run(coro)


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.pipe, self.db, self.trello, self.gateway, self.pending = make_pipeline()
        self.user = "u1"
        self.sent = self.gateway.sent

    def last_message(self):
        return self.sent[-1][1]

    def say(self, msg_id, text):
        run(self.pipe.handle_incoming(self.user, msg_id, text))

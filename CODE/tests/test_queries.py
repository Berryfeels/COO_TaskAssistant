from tests.base import BaseCase

from app.orchestrator.agent import MockLLM
from app.trello.client import FakeTrello


class NoEmptySearchTrello(FakeTrello):
    """Real Trello rejects /1/search with an empty query (HTTP 400): the
    query path must never depend on it. Regression: 'list all my tasks'
    used to crash silently on the live client."""

    def search_cards(self, query):
        if not query.strip():
            raise RuntimeError("HTTP 400: invalid value for query")
        return super().search_cards(query)


class ExplodingLLM:
    """Any non-Clarification failure from the orchestrator (API down,
    network error, bad JSON) must reach the user, not the log only."""

    def interpret(self, text, user, trello):
        raise RuntimeError("API down")


class TestQueries(BaseCase):
    def setUp(self):
        super().setUp()
        self.pipe.trello = NoEmptySearchTrello()

    def test_list_my_tasks_answers(self):
        self.say("m1", "create task 'Call supplier' for marco")
        self.say("m2", "create task 'Send invoice' for anna")
        self.say("m3", "what are my tasks")
        msg = self.last_message()
        self.assertIn("Your open tasks", msg)
        self.assertIn("Call supplier", msg)
        self.assertIn("Send invoice", msg)

    def test_empty_board_answers_no_open_tasks(self):
        self.say("m1", "what are my tasks")
        self.assertEqual(self.last_message(), "You have no open tasks.")
        self.assertTrue(self.db.already_processed("m1"))

    def test_llm_failure_gets_a_reply_not_silence(self):
        self.pipe.llm = ExplodingLLM()
        self.say("m1", "mandami una lista di tutte le tasks")
        self.assertIn("something went wrong", self.last_message())
        # the pipeline is still alive
        self.pipe.llm = MockLLM()
        self.say("m2", "create task 'Call supplier' for marco")
        self.assertIn("created in Trello", self.last_message())

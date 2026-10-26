"""Offline tests for MistralLLM: response parsing, tool dispatch, the
tool-use loop, and the exact HTTP request sent to the Mistral API.

No network and no API key: urlopen / _complete are stubbed.
"""
import json
import unittest
from unittest import mock

from app.orchestrator.mistral_llm import API_URL, MistralLLM
from app.schema import Action, Clarification, Task
from app.trello.client import FakeTrello


class FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def completion(content=None, tool_calls=None):
    return {"choices": [{"message": {"content": content,
                                     "tool_calls": tool_calls}}]}


def tool_call(name, args="{}", call_id="t1"):
    return {"id": call_id, "function": {"name": name, "arguments": args}}


class TestParse(unittest.TestCase):
    def setUp(self):
        self.llm = MistralLLM(api_key="k", model="m")
        self.user = {"source_message_id": "m1"}

    def parse(self, content):
        return self.llm._parse(content, self.user, None)

    def test_plain_json(self):
        task = self.parse('{"action": "create", "title": "Call supplier",'
                          ' "due": "2026-11-01T10:00:00+01:00"}')
        self.assertEqual(task.action, Action.create)
        self.assertEqual(task.title, "Call supplier")
        self.assertEqual(task.due, "2026-11-01T10:00:00+01:00")
        self.assertEqual(task.source_message_id, "m1")

    def test_fenced_json(self):
        task = self.parse("```json\n{\"action\": \"query\"}\n```")
        self.assertEqual(task.action, Action.query)

    def test_json_inside_prose(self):
        task = self.parse('Sure! Here it is:\n{"action": "archive",'
                          ' "target_card_id": "card1"}\nAnything else?')
        self.assertEqual(task.action, Action.archive)
        self.assertEqual(task.target_card_id, "card1")

    def test_missing_json_raises_clarification(self):
        with self.assertRaises(Clarification):
            self.parse("I have no idea what you mean.")

    def test_question_raises_clarification_with_that_text(self):
        with self.assertRaises(Clarification) as ctx:
            self.parse('{"action": "create", "title": "x",'
                       ' "confidence": 0.4, "question": "Which Marco?"}')
        self.assertEqual(ctx.exception.question, "Which Marco?")

    def test_source_message_id_defaults_from_user(self):
        task = self.parse('{"action": "query"}')
        self.assertEqual(task.source_message_id, "m1")

    def test_unknown_fields_are_dropped(self):
        task = self.parse('{"action": "query", "bogus_field": "x"}')
        self.assertEqual(task.action, Action.query)
        self.assertFalse(hasattr(task, "bogus_field"))

    def test_missing_action_defaults_to_query(self):
        task = self.parse('{"title": "whatever"}')
        self.assertEqual(task.action, Action.query)


class TestCallTool(unittest.TestCase):
    def setUp(self):
        self.llm = MistralLLM(api_key="k")
        self.trello = FakeTrello()
        self.trello.create_card(
            Task(action=Action.create, title="Call supplier"))

    def test_board_cards(self):
        cards = self.llm._call_tool("board_cards", {}, self.trello)
        self.assertEqual([c["name"] for c in cards], ["Call supplier"])

    def test_search_cards(self):
        found = self.llm._call_tool("search_cards", {"query": "supplier"},
                                     self.trello)
        self.assertEqual(len(found), 1)

    def test_get_card(self):
        card = self.llm._call_tool("get_card", {"card_id": "card1"},
                                    self.trello)
        self.assertEqual(card["name"], "Call supplier")

    def test_unknown_tool_raises(self):
        with self.assertRaises(ValueError):
            self.llm._call_tool("nope", {}, self.trello)


class TestCompleteRequest(unittest.TestCase):
    def test_request_shape(self):
        llm = MistralLLM(api_key="k", model="mistral-small-latest")
        with mock.patch("urllib.request.urlopen") as up:
            up.return_value = FakeResponse(completion(content="{}"))
            llm._complete([{"role": "user", "content": "hi"}])

        req = up.call_args[0][0]
        self.assertEqual(req.full_url, API_URL)
        self.assertEqual(req.method, "POST")
        self.assertEqual(req.get_header("Authorization"), "Bearer k")
        body = json.loads(req.data)
        self.assertEqual(body["model"], "mistral-small-latest")
        self.assertEqual(body["temperature"], 0.2)
        self.assertEqual([t["function"]["name"] for t in body["tools"]],
                         ["board_cards", "search_cards", "get_card",
                          "list_members"])
        # response_format must stay off: with tools it makes the model loop
        self.assertNotIn("response_format", body)


class TestInterpretLoop(unittest.TestCase):
    def setUp(self):
        self.llm = MistralLLM(api_key="k")

    def test_tool_result_feeds_the_final_answer(self):
        # _complete returns the unwrapped assistant message (not the envelope)
        responses = iter([
            {"tool_calls": [tool_call("list_members")]},
            {"content": '{"action": "create", "title": "Call Marco",'
                        ' "assignees": ["m1"]}'},
        ])
        seen = []

        def fake_complete(messages):
            seen.append(messages[-1])
            return next(responses)

        self.llm._complete = fake_complete
        task = self.llm.interpret("call Marco", {"source_message_id": "m1"},
                                  FakeTrello())
        # the tool round-trip carried the member list as a tool message
        tool_msg = seen[1]          # seen[0] is the original user message
        self.assertEqual(tool_msg["role"], "tool")
        self.assertIn("Marco", tool_msg["content"])
        self.assertEqual(task.assignees, ["m1"])

    def test_endless_tool_calls_raise_clarification(self):
        loops = iter({"tool_calls": [tool_call("list_members")]}
                     for _ in range(10))
        self.llm._complete = lambda messages: next(loops)
        with self.assertRaises(Clarification):
            self.llm.interpret("call Marco", {}, FakeTrello())

    def test_direct_answer_never_touches_trello(self):
        trello = FakeTrello()
        self.llm._complete = lambda messages: {
            "content": '{"action": "create", "title": "X"}'}
        task = self.llm.interpret("new thing", {"source_message_id": "m9"},
                                  trello)
        self.assertEqual(task.action, Action.create)
        self.assertEqual(trello.cards, {})   # no reads, no writes

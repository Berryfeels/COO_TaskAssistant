"""Unit tests for app/orchestrator/mistral_llm.py: the LLM answers in plain
words and the deterministic WordParser fills the Card JSON (the LLM never
emits JSON). Covers tool dispatch (_call_tool), the bounded tool loop and
per-user history in interpret(), and the exact API request shape.
No network: _complete and urllib.request.urlopen are stubbed."""
import json
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from app.orchestrator.mistral_llm import API_URL, MistralLLM
from app.schema import Action, Clarification
from CODE.tests.SHARED.base import FakeTrello

TZ = ZoneInfo("Europe/Berlin")
TODAY = datetime(2026, 11, 2, 10, 0, tzinfo=TZ)   # a Monday


def llm():
    return MistralLLM(api_key="k", model="m")


class TestWordsToCard(unittest.TestCase):
    """interpret() hands the LLM's plain words to the parser."""

    def setUp(self):
        self.llm = llm()
        self.trello = FakeTrello()
        self.user = {"user_id": "u", "source_message_id": "m1"}

    def stub(self, responses):
        responses = list(responses)
        self.llm._complete = lambda messages: responses.pop(0)

    def test_labelled_words_become_a_card(self):
        self.stub([{"content": "action: add card\ntitle: Call supplier"}])
        card = self.llm.interpret("add a card", self.user, self.trello)
        self.assertEqual(card.action, Action.add_card)
        self.assertEqual(card.title, "Call supplier")
        self.assertEqual(card.source_message_id, "m1")

    def test_prose_becomes_a_chat_reply(self):
        self.stub([{"content": "Ciao! Come posso aiutarti?"}])
        card = self.llm.interpret("ciao", self.user, self.trello)
        self.assertEqual(card.action, Action.chat)
        self.assertEqual(card.reply, "Ciao! Come posso aiutarti?")

    def test_question_line_raises_clarification(self):
        self.stub([{"content": "question: Which card do you mean?"}])
        with self.assertRaises(Clarification) as ctx:
            self.llm.interpret("do it", self.user, self.trello)
        self.assertEqual(str(ctx.exception), "Which card do you mean?")

    def test_empty_answer_raises_clarification(self):
        self.stub([{"content": "   "}])
        with self.assertRaises(Clarification):
            self.llm.interpret("???", self.user, self.trello)

    def test_json_output_is_treated_as_prose_never_parsed(self):
        # the old contract (LLM emits JSON) is gone: JSON-looking content is
        # just words, so it degrades to a chat reply, not to an action
        self.stub([{"content": '{"action": "add_card", "title": "X"}'}])
        card = self.llm.interpret("add X", self.user, self.trello)
        self.assertEqual(card.action, Action.chat)
        self.assertEqual(self.trello.calls, [])


class TestCallTool(unittest.TestCase):
    def setUp(self):
        self.llm = llm()
        self.trello = FakeTrello()
        self.trello.cards = [{"id": "c1", "name": "Call supplier"}]

    def test_known_tools_dispatch_to_trello(self):
        self.assertEqual(self.llm._call_tool("board_cards", {}, self.trello),
                         self.trello.cards)
        self.assertEqual(self.llm._call_tool("search_cards", {"query": "call"},
                                             self.trello), self.trello.cards)
        self.assertEqual(self.llm._call_tool("get_card", {"card_id": "c1"},
                                             self.trello)["name"],
                         "Call supplier")
        self.assertEqual(self.llm._call_tool("list_members", {}, self.trello),
                         self.trello.members)
        self.assertEqual(self.llm._call_tool("list_lists", {}, self.trello),
                         self.trello.lists)

    def test_unknown_tool_raises(self):
        with self.assertRaises(ValueError):
            self.llm._call_tool("delete_everything", {}, self.trello)


class TestUnknownToolRecovery(unittest.TestCase):
    """The model sometimes calls an ACTION name (e.g. "archive") as if it were
    a tool. That must be a soft tool error the model can self-correct from,
    never a crash; only if it keeps failing does the user get a problem-first
    message ('The tool "archive" is unknown, sorry. ...')."""

    def setUp(self):
        self.llm = llm()
        self.trello = FakeTrello()
        self.trello.cards = [{"id": "c9", "name": "Fusking task", "closed": False}]
        self.user = {"user_id": "u", "source_message_id": "m1"}

    def test_unknown_tool_is_answered_as_tool_error_and_llm_recovers(self):
        record = []
        responses = [
            {"content": None, "tool_calls": [
                {"id": "t1", "function": {"name": "archive",
                                          "arguments": "{}"}}]},
            {"content": "action: archive\ncard: the fusking task"},
        ]
        self.llm._complete = lambda messages: (
            record.append([dict(m) for m in messages]), responses.pop(0))[1]
        card = self.llm.interpret("Archive the fusking task", self.user,
                                  self.trello)
        # the model self-corrected: archive is an action on the action: line
        self.assertEqual(card.action, Action.archive)
        self.assertTrue(card.closed)
        self.assertEqual(card.target_card_id, "c9")
        # the unknown tool was answered as a tool error inside the loop
        tool_msgs = [m for m in record[1] if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        payload = json.loads(tool_msgs[0]["content"])
        self.assertIn("unknown tool", payload["error"])
        self.assertIn("NOT", payload["hint"])

    def test_repeated_unknown_tool_asks_the_user_problem_first(self):
        bad = {"content": None, "tool_calls": [
            {"id": "t", "function": {"name": "archive", "arguments": "{}"}}]}
        self.llm._complete = lambda messages: bad
        with self.assertRaises(Clarification) as ctx:
            self.llm.interpret("Archive the fusking task", self.user,
                               self.trello)
        msg = ctx.exception.question
        self.assertIn('The tool "archive" is unknown', msg)
        self.assertIn("sorry", msg)
        self.assertIn("Please write something else", msg)


class TestToolFailureRecovery(unittest.TestCase):
    """A failing READ tool (e.g. get_card called with a hallucinated ID ->
    HTTP 400/404) must be a soft tool error the model can self-correct from,
    never a crash of the whole interpretation."""

    def setUp(self):
        self.llm = llm()
        self.trello = FakeTrello()
        self.user = {"user_id": "u", "source_message_id": "m1"}

    def test_failed_tool_is_answered_as_tool_error_and_llm_recovers(self):
        def bad_get_card(card_id):
            raise RuntimeError("HTTP Error 404: Not Found")
        self.trello.get_card = bad_get_card
        record = []
        responses = [
            {"content": None, "tool_calls": [
                {"id": "t1", "function": {"name": "get_card",
                                          "arguments": '{"card_id": "685404550a598d4edbc7fb"}'}}]},
            {"content": "question: Which card do you mean? I only see names, not IDs."},
        ]
        self.llm._complete = lambda messages: (
            record.append([dict(m) for m in messages]), responses.pop(0))[1]
        with self.assertRaises(Clarification) as ctx:
            self.llm.interpret("show me that card", self.user, self.trello)
        self.assertIn("Which card do you mean?", ctx.exception.question)
        tool_msgs = [m for m in record[1] if m.get("role") == "tool"]
        payload = json.loads(tool_msgs[0]["content"])
        self.assertIn("failed", payload["error"])
        self.assertIn("invented IDs", payload["hint"])

    def test_loop_exhaustion_with_tool_errors_reraises_the_error(self):
        def bad_cards():
            raise RuntimeError("HTTP Error 400: Bad Request")
        self.trello.board_cards = bad_cards
        bad = {"content": None, "tool_calls": [
            {"id": "t", "function": {"name": "board_cards", "arguments": "{}"}}]}
        self.llm._complete = lambda messages: bad
        with self.assertRaises(RuntimeError) as ctx:
            self.llm.interpret("my cards", self.user, self.trello)
        self.assertIn("HTTP Error 400", str(ctx.exception))


class TestAssigneeFallbackFromRawText(unittest.TestCase):
    """'crea una task per marco' must end up with Marco as assignee even if
    the LLM forgot the assign: line - the parser scans the raw user text."""

    def test_member_in_raw_text_assigned_despite_missing_assign_line(self):
        m = llm()
        trello = FakeTrello()
        m._complete = lambda messages: {
            "content": "action: add_card\ntitle: Roll a joint"}
        card = m.interpret("crea una task per marco: roll a joint",
                           {"user_id": "u", "source_message_id": "m1"}, trello)
        self.assertEqual(card.action, Action.add_card)
        self.assertEqual(card.assignees, ["m2"])       # Marco Rossi


class TestInterpretLoop(unittest.TestCase):
    def setUp(self):
        self.llm = llm()
        self.trello = FakeTrello()
        self.user = {"user_id": "u1", "source_message_id": "m1"}

    def stub_complete(self, responses, record=None):
        responses = list(responses)

        def fake_complete(messages):
            if record is not None:
                record.append([dict(m) for m in messages])
            return responses.pop(0)

        self.llm._complete = fake_complete

    def test_direct_answer_never_touches_trello(self):
        self.stub_complete([{"content": "Ciao!"}])
        card = self.llm.interpret("ciao", self.user, self.trello)
        self.assertEqual(card.action, Action.chat)
        self.assertEqual(card.reply, "Ciao!")
        self.assertEqual(self.trello.calls, [])

    def test_tool_result_feeds_the_final_words(self):
        record = []
        self.stub_complete([
            {"content": None, "tool_calls": [
                {"id": "t1", "function": {"name": "board_cards",
                                          "arguments": "{}"}}]},
            {"content": "action: add card\ntitle: X"},
        ], record)
        card = self.llm.interpret("add X", self.user, self.trello)
        self.assertEqual(card.action, Action.add_card)
        self.assertEqual(card.title, "X")
        # board_cards from the tool call + list_members from the assignee
        # fallback scanning the raw user text (no member named in "add X")
        self.assertEqual(self.trello.calls, [("board_cards",), ("list_members",)])
        self.assertEqual(card.assignees, [])
        # the tool result was appended to the conversation before the answer
        tool_msgs = [m for m in record[1] if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertEqual(tool_msgs[0]["tool_call_id"], "t1")
        self.assertEqual(json.loads(tool_msgs[0]["content"]), [])

    def test_endless_tool_calls_raise_clarification(self):
        tool_msg = {"content": None, "tool_calls": [
            {"id": "t", "function": {"name": "board_cards", "arguments": "{}"}}]}
        self.stub_complete([tool_msg] * 10)
        with self.assertRaises(Clarification):
            self.llm.interpret("loop", self.user, self.trello)

    def test_history_is_capped_at_max_turns(self):
        self.stub_complete([{"content": "ok"}] * 10)
        for i in range(10):
            self.llm.interpret(f"msg {i}", self.user, self.trello)
        self.assertEqual(len(self.llm._history["u1"]), self.llm.MAX_HISTORY)

    def test_conversation_history_is_per_user(self):
        self.stub_complete([{"content": "a"}, {"content": "b"}])
        self.llm.interpret("hi", {"user_id": "u1", "source_message_id": "m1"},
                           self.trello)
        self.llm.interpret("hi", {"user_id": "u2", "source_message_id": "m2"},
                           self.trello)
        self.assertEqual(set(self.llm._history), {"u1", "u2"})
        self.assertEqual(len(self.llm._history["u1"]), 2)
        self.assertEqual(len(self.llm._history["u2"]), 2)


class _Resp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestCompleteRequest(unittest.TestCase):
    def test_request_shape(self):
        captured = {}

        def fake_urlopen(req, *a, **kw):
            captured["req"] = req
            return _Resp({"choices": [
                {"message": {"content": "Ciao!"}}]})

        m = llm()
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            card = m.interpret("ciao", {"user_id": "u1", "source_message_id": "m1"},
                               FakeTrello())
        self.assertEqual(card.reply, "Ciao!")
        req = captured["req"]
        self.assertEqual(req.full_url, API_URL)
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"), "Bearer k")
        self.assertEqual(req.get_header("Content-type"), "application/json")
        body = json.loads(req.data)
        self.assertEqual(body["model"], "m")
        tool_names = {t["function"]["name"] for t in body["tools"]}
        self.assertEqual(tool_names, {"board_cards", "search_cards", "get_card",
                                      "list_members", "list_lists"})
        prompt = body["messages"][0]["content"]
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertIn("Today is", prompt)
        self.assertIn("Europe/Berlin", prompt)
        # the prompt demands plain words, not JSON, from the model
        self.assertIn("NEVER output JSON", prompt)
        self.assertIn("action:", prompt)
        # and embeds the actions_list.txt catalogue: the LLM may only pick
        # actions from that list, never invent them
        self.assertIn("actions_list.txt", prompt)
        self.assertIn("add_card", prompt)
        self.assertIn("query", prompt)
        # and the prompt forbids calling catalogue actions as tools
        self.assertIn("NEVER tools", prompt)
        self.assertIn("archive", prompt)
        # forbids invented IDs and demands the assign: line for "task for X"
        self.assertIn("NEVER invent", prompt)
        self.assertIn("assign:", prompt)
        self.assertIn("No markdown", prompt)
        self.assertEqual(body["messages"][-1],
                         {"role": "user", "content": "ciao"})
        # no response_format: combined with tools it loops forever (see code)
        self.assertNotIn("response_format", body)


if __name__ == "__main__":
    unittest.main()

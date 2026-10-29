"""The LLM owns the conversation: free-text chat replies are forwarded
to the user as-is, with no Trello involvement."""
from tests.base import BaseCase

from app.schema import Action, Task


class ChatLLM:
    """Returns a fixed chat Task and records what the pipeline passed."""

    def __init__(self, reply):
        self.task = Task(action=Action.chat, reply=reply)
        self.seen_user = None

    def interpret(self, text, user, trello):
        self.seen_user = user
        return self.task


class TestChat(BaseCase):
    def test_free_reply_forwarded_verbatim(self):
        self.pipe.llm = ChatLLM("Ciao! Come posso aiutarti?")
        self.say("m1", "ciao")
        self.assertEqual(self.last_message(), "Ciao! Come posso aiutarti?")
        self.assertEqual(len(self.trello.cards), 0)
        self.assertTrue(self.db.already_processed("m1"))

    def test_empty_reply_gets_a_fallback(self):
        self.pipe.llm = ChatLLM("   ")
        self.say("m1", "ciao")
        self.assertEqual(self.last_message(),
                         "I'm here - what would you like me to do?")

    def test_user_id_reaches_the_llm(self):
        llm = ChatLLM("Ciao!")
        self.pipe.llm = llm
        self.say("m1", "ciao")
        self.assertEqual(llm.seen_user.get("user_id"), self.user)

    def test_chat_does_not_stop_the_next_write(self):
        self.pipe.llm = ChatLLM("Ciao!")
        self.say("m1", "ciao")
        from app.orchestrator.agent import MockLLM
        self.pipe.llm = MockLLM()
        self.say("m2", "create task 'Call supplier' for marco")
        self.assertIn("created in Trello", self.last_message())

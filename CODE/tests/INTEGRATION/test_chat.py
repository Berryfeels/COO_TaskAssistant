"""Integration: free conversation is owned by the LLM (action chat) - the
reply is forwarded verbatim, nothing is written to Trello, and the next
message is processed normally."""
import unittest

from CODE.tests.SHARED.base import add, chat, incoming, last_reply, make_pipeline


class TestChat(unittest.TestCase):
    def test_free_reply_forwarded_verbatim(self):
        h = make_pipeline(script=[chat("Ciao! Come posso aiutarti?")])
        incoming(h.pipe, "u", "m1", "ciao!")
        self.assertEqual(last_reply(h.gateway), "Ciao! Come posso aiutarti?")
        self.assertEqual(h.trello.calls, [])
        self.assertTrue(h.db.already_processed("m1"))

    def test_empty_reply_gets_a_fallback(self):
        h = make_pipeline(script=[chat("   ")])
        incoming(h.pipe, "u", "m1", "hello?")
        self.assertEqual(last_reply(h.gateway),
                         "I'm here - what would you like me to do?")

    def test_user_id_reaches_the_llm(self):
        h = make_pipeline(script=[chat("hi")])
        incoming(h.pipe, "stefano", "m1", "hello")
        text, user = h.llm.calls[0]
        self.assertEqual(user["user_id"], "stefano")
        self.assertEqual(user["source_message_id"], "m1")

    def test_chat_does_not_stop_the_next_write(self):
        h = make_pipeline(script=[chat("hey"), add("X")])
        incoming(h.pipe, "u", "m1", "ciao")
        incoming(h.pipe, "u", "m2", "add X")
        self.assertEqual(last_reply(h.gateway),
                         "Done: 'X' created in Trello.")
        self.assertEqual(len(h.trello.cards), 1)


if __name__ == "__main__":
    unittest.main()

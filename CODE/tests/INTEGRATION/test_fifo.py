"""Integration: FIFO ingestion (spec 5.3) and legacy confirmation binding
through the real Pipeline - messages arrive via handle_incoming, exactly as
the SimpleX gateway delivers them."""
import unittest

from CODE.tests.SHARED.base import add, chat, incoming, last_reply, make_pipeline, run


class TestFIFO(unittest.TestCase):
    def test_queue_drains_in_arrival_order(self):
        h = make_pipeline(script=[chat("1"), chat("2"), chat("3")])
        h.db.queue_push("u", "m1", "first")
        h.db.queue_push("u", "m2", "second")
        h.db.queue_push("u", "m3", "third")
        run(h.pipe.drain("u"))
        self.assertEqual([t for t, _ in h.llm.calls],
                         ["first", "second", "third"])

    def test_queue_is_per_user(self):
        h = make_pipeline(script=[chat()])
        h.db.queue_push("u1", "m1", "for u1")
        h.db.queue_push("u2", "m2", "for u2")
        run(h.pipe.drain("u1"))
        self.assertEqual([t for t, _ in h.llm.calls], ["for u1"])
        self.assertEqual(h.db.queue_pop("u2"), ("m2", "for u2"))

    def test_each_message_is_an_independent_process(self):
        # a chat message neither blocks nor consumes the next write
        h = make_pipeline(script=[chat("Ciao!"), add("Call supplier")])
        incoming(h.pipe, "u", "m1", "ciao")
        incoming(h.pipe, "u", "m2", "add: call supplier")
        self.assertEqual(last_reply(h.gateway, "u"),
                         "Done: 'Call supplier' created in Trello.")
        self.assertEqual([c["name"] for c in h.trello.cards], ["Call supplier"])

    def test_write_applied_without_confirmation(self):
        # since 2026-10-29 there is no confirmation loop: one message, one write
        h = make_pipeline(script=[add("X")])
        incoming(h.pipe, "u", "m1", "add X")
        self.assertEqual(len(h.trello.cards), 1)
        self.assertEqual(h.pending.active("u"), [])
        self.assertIn("Done:", last_reply(h.gateway))
        self.assertNotIn("confirm", last_reply(h.gateway))

    def test_back_to_back_messages_keep_arrival_order(self):
        h = make_pipeline(script=[add("A"), add("B")])
        incoming(h.pipe, "u", "m1", "add A")
        incoming(h.pipe, "u", "m2", "add B")
        self.assertEqual([c["name"] for c in h.trello.cards], ["A", "B"])


class TestLegacyConfirmationBinding(unittest.TestCase):
    """Pendings created before 2026-10-29 are still drained: a confirmation
    is bound to its pending_id (spec 5.3)."""

    def _with_pendings(self, n=2):
        h = make_pipeline()
        pids = [h.db.pending_save("u", add(f"card {i}"), 1000.0 + i)
                for i in range(n)]
        return h, pids

    def test_ok_with_id_applies_only_that_pending(self):
        h, (p1, p2) = self._with_pendings(2)
        incoming(h.pipe, "u", "m1", f"OK {p1}")
        self.assertEqual([c["name"] for c in h.trello.cards], ["card 0"])
        self.assertEqual([r.pending_id for r in h.db.pending_active("u")], [p2])
        status = h.db.conn.execute(
            "SELECT status FROM pending WHERE pending_id=?", (p1,)).fetchone()
        self.assertEqual(status["status"], "applied")
        self.assertIn("Done:", last_reply(h.gateway))

    def test_bare_ok_with_single_pending_applies_it(self):
        h, (p1,) = self._with_pendings(1)
        incoming(h.pipe, "u", "m1", "OK")
        self.assertEqual(len(h.trello.cards), 1)
        self.assertEqual(h.trello.cards[0]["name"], "card 0")

    def test_bare_ok_with_multiple_pendings_is_ambiguous(self):
        h, (p1, p2) = self._with_pendings(2)
        incoming(h.pipe, "u", "m1", "OK")
        self.assertEqual(h.trello.cards, [])          # never guesses
        msg = last_reply(h.gateway)
        self.assertIn("Which one do you mean?", msg)
        self.assertIn(p1, msg)
        self.assertIn(p2, msg)
        self.assertEqual(len(h.db.pending_active("u")), 2)

    def test_stray_ok_without_pendings_goes_to_the_llm_and_writes_nothing(self):
        h = make_pipeline(script=[chat("I heard OK")])
        incoming(h.pipe, "u", "m1", "OK")
        self.assertEqual(h.trello.calls, [])
        self.assertEqual(last_reply(h.gateway), "I heard OK")

    def test_pending_id_without_affirmative_is_a_new_instruction(self):
        h, (p1,) = self._with_pendings(1)
        h.llm.script.append(add("fresh card"))
        incoming(h.pipe, "u", "m1", p1)   # mentions the id, no OK/YES
        self.assertEqual([c["name"] for c in h.trello.cards], ["fresh card"])
        self.assertEqual(len(h.db.pending_active("u")), 1)  # original intact


if __name__ == "__main__":
    unittest.main()

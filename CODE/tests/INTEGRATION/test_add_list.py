"""Integration: add_list (spec V6) - a column is always created, an unnamed
one defaults to "new List", and the local lists mirror is written only AFTER
the Trello create_list succeeded."""
import unittest

from app.schema import Action, Card
from CODE.tests.SHARED.base import add, incoming, last_reply, make_pipeline


class TestAddListValidation(unittest.TestCase):
    def test_is_write_action(self):
        self.assertTrue(Card(action=Action.add_list).is_write)

    def test_no_target_card_needed(self):
        Card(action=Action.add_list, title="Blocked").validate()

    def test_name_optional_defaults_to_new_list(self):
        for title in ("", None):
            c = Card(action=Action.add_list, title=title)
            c.validate()
            self.assertEqual(c.title, "new List")


class TestAddListPipeline(unittest.TestCase):
    def test_column_created_on_trello_and_mirrored_in_db(self):
        h = make_pipeline(script=[Card(action=Action.add_list, title="Review")])
        incoming(h.pipe, "u", "m1", "add a column called Review")
        self.assertIn(("create_list", "Review"), h.trello.calls)
        rows = h.db.list_all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Review")
        self.assertEqual(rows[0]["list_id"], h.trello.lists[-1]["id"])
        self.assertEqual(last_reply(h.gateway),
                         "Done: column 'Review' added to the board in Trello.")

    def test_unnamed_column_defaults_to_new_list(self):
        h = make_pipeline(script=[Card(action=Action.add_list, title="")])
        incoming(h.pipe, "u", "m1", "add a column")
        self.assertIn(("create_list", "new List"), h.trello.calls)
        self.assertEqual(h.db.list_all()[0]["name"], "new List")
        self.assertIn("'new List'", last_reply(h.gateway))

    def test_db_mirror_not_written_when_trello_fails(self):
        h = make_pipeline(script=[Card(action=Action.add_list, title="Review")])
        h.trello.fail_next()
        incoming(h.pipe, "u", "m1", "add column Review")
        self.assertEqual(h.db.list_all(), [])         # DB follows Trello
        self.assertEqual(len(h.trello.lists), 3)      # only the seeded ones
        self.assertIn("could not write", last_reply(h.gateway))
        self.assertTrue(h.db.already_processed("m1"))

    def test_new_column_usable_for_moves(self):
        # the LLM resolves the new column name via list_lists(); the pipeline
        # then moves a card into it by list_id
        h = make_pipeline(script=[Card(action=Action.add_list, title="Blocked"),
                                  Card(action=Action.update,
                                       target_card_id="c1", list_id="?")])
        h.trello.cards = [{"id": "c1", "name": "Fix login", "idList": "Ltodo",
                           "due": None, "dueComplete": False, "closed": False}]
        incoming(h.pipe, "u", "m1", "add column Blocked")
        new_id = h.trello.lists[-1]["id"]
        self.assertIn({"id": new_id, "name": "Blocked"},
                      [{"id": l["id"], "name": l["name"]}
                       for l in h.trello.list_lists()])
        h.llm.script[0].list_id = new_id              # what the LLM resolves
        incoming(h.pipe, "u", "m2", "move Fix login to Blocked")
        self.assertEqual(h.trello.cards[0]["idList"], new_id)
        self.assertIn("updated", last_reply(h.gateway))


class TestListStorage(unittest.TestCase):
    def test_save_get_all(self):
        h = make_pipeline()
        h.db.list_save("L9", "Review", 100.0)
        self.assertEqual(h.db.list_get("L9")["name"], "Review")
        self.assertEqual([r["list_id"] for r in h.db.list_all()], ["L9"])


if __name__ == "__main__":
    unittest.main()

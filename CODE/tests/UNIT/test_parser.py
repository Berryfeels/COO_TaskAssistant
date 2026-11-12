"""Unit tests for app/orchestrator/parser.py: the deterministic WordParser
that fills the Card JSON from the LLM's plain words. The LLM never emits
JSON - these tests pin the words -> Card contract, including name/date
resolution against the board and the Clarification-on-ambiguity policy."""
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from app.orchestrator.parser import WordParser
from app.schema import Action, Clarification
from CODE.tests.SHARED.base import FakeTrello

TZ = ZoneInfo("Europe/Berlin")
TODAY = datetime(2026, 11, 2, 10, 0, tzinfo=TZ)   # a Monday, CET (+01:00)


class ParserCase(unittest.TestCase):
    def setUp(self):
        self.p = WordParser()
        self.trello = FakeTrello()
        self.user = {"user_id": "u", "source_message_id": "m1"}

    def parse(self, words, trello=None):
        return self.p.parse(words, self.user, TODAY, trello or self.trello)


class TestAddCard(ParserCase):
    def test_full_labelled_words(self):
        c = self.parse("action: add card\n"
                       "title: Call the supplier\n"
                       "assign: Stefano\n"
                       "due: next Friday at 9:00\n"
                       "list: To-do\n"
                       "note: dairy-free cake, contact Carla")
        self.assertEqual(c.action, Action.add_card)
        self.assertEqual(c.title, "Call the supplier")
        self.assertEqual(c.assignees, ["m1"])
        self.assertEqual(c.due, "2026-11-06T09:00:00+01:00")
        self.assertEqual(c.list_id, "Ltodo")
        self.assertEqual(c.description, "dairy-free cake, contact Carla")
        self.assertEqual(c.source_message_id, "m1")

    def test_quoted_title_fallback(self):
        c = self.parse('action: add card "Call supplier"')
        self.assertEqual(c.action, Action.add_card)
        self.assertEqual(c.title, "Call supplier")

    def test_multiple_assignees_resolve_to_ids(self):
        c = self.parse("action: add card\ntitle: X\nassign: Stefano and Marco")
        self.assertEqual(c.assignees, ["m1", "m2"])

    def test_unknown_assignee_asks_with_member_list(self):
        with self.assertRaises(Clarification) as ctx:
            self.parse("action: add card\ntitle: X\nassign: Giulia")
        self.assertIn("Giulia", str(ctx.exception))
        self.assertIn("Stefano Di Puma", str(ctx.exception))

    def test_list_matches_ignoring_case(self):
        c = self.parse("action: add card\ntitle: X\nlist: DONE")
        self.assertEqual(c.list_id, "Ldone")

    def test_list_matches_ignoring_emoji(self):
        self.trello.lists[2]["name"] = "Done 🎉"
        c = self.parse("action: add card\ntitle: X\nlist: done")
        self.assertEqual(c.list_id, "Ldone")

    def test_unknown_list_asks_with_available_columns(self):
        with self.assertRaises(Clarification) as ctx:
            self.parse("action: add card\ntitle: X\nlist: Blocked")
        self.assertIn("no column", str(ctx.exception))
        self.assertIn("To-do", str(ctx.exception))

    def test_add_card_without_title_asks(self):
        with self.assertRaises(Clarification):
            self.parse("action: add card")


class TestChatAndQuery(ParserCase):
    def test_plain_prose_is_a_chat_reply_and_touches_nothing(self):
        c = self.parse("Ciao, come stai?")
        self.assertEqual(c.action, Action.chat)
        self.assertEqual(c.reply, "Ciao, come stai?")
        self.assertEqual(c.source_message_id, "m1")
        self.assertEqual(self.trello.calls, [])

    def test_reply_label(self):
        c = self.parse("reply: Ciao!")
        self.assertEqual(c.action, Action.chat)
        self.assertEqual(c.reply, "Ciao!")

    def test_question_line_becomes_clarification(self):
        with self.assertRaises(Clarification) as ctx:
            self.parse("action: update card\nquestion: Which card do you mean?")
        self.assertEqual(str(ctx.exception), "Which card do you mean?")

    def test_empty_words_raise_clarification(self):
        with self.assertRaises(Clarification):
            self.parse("   ")

    def test_show_cards_is_a_query(self):
        c = self.parse("action: show cards")
        self.assertEqual(c.action, Action.query)

    def test_json_looking_text_is_never_parsed_as_json(self):
        # regression guard for the old contract: if the model still emits
        # JSON, it is treated as prose (chat), never parsed into an action
        c = self.parse('{"action": "add_card", "title": "X"}')
        self.assertEqual(c.action, Action.chat)
        self.assertIn("add_card", c.reply)


class TestModifyActions(ParserCase):
    def setUp(self):
        super().setUp()
        self.trello.cards = [
            {"id": "c1", "name": "Call supplier", "closed": False},
            {"id": "c2", "name": "Prepare report", "closed": False},
        ]

    def test_mark_done_sets_done_and_done_column(self):
        c = self.parse("action: mark done\ncard: Call supplier")
        self.assertEqual(c.action, Action.complete)
        self.assertEqual(c.target_card_id, "c1")
        self.assertTrue(c.done)
        self.assertEqual(c.list_id, "Ldone")

    def test_update_with_list(self):
        c = self.parse("action: update card\ncard: Prepare report\nlist: Doing")
        self.assertEqual(c.action, Action.update)
        self.assertEqual(c.target_card_id, "c2")
        self.assertEqual(c.list_id, "Ldoing")

    def test_move_to_column_parsed_from_action_text(self):
        c = self.parse("action: move card to Doing\ncard: Prepare report")
        self.assertEqual(c.action, Action.update)
        self.assertEqual(c.list_id, "Ldoing")

    def test_archive_sets_closed(self):
        c = self.parse("action: archive card\ncard: Call supplier")
        self.assertEqual(c.action, Action.archive)
        self.assertEqual(c.target_card_id, "c1")
        self.assertTrue(c.closed)

    def test_move_date(self):
        c = self.parse("action: move date\ncard: Call supplier\n"
                       "due: in 3 days at 8")
        self.assertEqual(c.action, Action.move_date)
        self.assertEqual(c.target_card_id, "c1")
        self.assertEqual(c.due, "2026-11-05T08:00:00+01:00")

    def test_move_date_without_due_asks(self):
        with self.assertRaises(Clarification) as ctx:
            self.parse("action: move date\ncard: Call supplier")
        self.assertIn("due date", str(ctx.exception))

    def test_missing_card_reference_lists_open_cards(self):
        with self.assertRaises(Clarification) as ctx:
            self.parse("action: mark done")
        self.assertIn("Call supplier", str(ctx.exception))

    def test_unknown_card_asks(self):
        with self.assertRaises(Clarification):
            self.parse("action: mark done\ncard: nonexistent thing entirely")

    def test_ambiguous_card_asks_instead_of_guessing(self):
        self.trello.cards.append(
            {"id": "c3", "name": "Call suppliers", "closed": False})
        with self.assertRaises(Clarification) as ctx:
            self.parse("action: mark done\ncard: Call supplier")
        self.assertIn("Which card", str(ctx.exception))

    def test_fuzzy_card_match_resolves(self):
        c = self.parse("action: mark done\ncard: call the supplier")
        self.assertEqual(c.target_card_id, "c1")


class TestAddList(ParserCase):
    def test_add_column_uses_title(self):
        c = self.parse("action: add column\ntitle: Blocked")
        self.assertEqual(c.action, Action.add_list)
        self.assertEqual(c.title, "Blocked")


class TestDates(ParserCase):
    def due(self, value):
        return self.p._parse_due(value, TODAY)

    def test_iso_passthrough(self):
        self.assertEqual(self.due("2026-11-17T08:00:00+01:00"),
                         "2026-11-17T08:00:00+01:00")

    def test_naive_iso_gets_berlin_offset(self):
        self.assertEqual(self.due("2026-11-17T08:00:00"),
                         "2026-11-17T08:00:00+01:00")

    def test_bare_weekday_is_date_only(self):
        self.assertEqual(self.due("Friday"), "2026-11-06")

    def test_tomorrow(self):
        self.assertEqual(self.due("tomorrow"), "2026-11-03")

    def test_next_weekday_with_time(self):
        self.assertEqual(self.due("next Friday at 9:00"),
                         "2026-11-06T09:00:00+01:00")

    def test_day_and_month(self):
        self.assertEqual(self.due("17 november"), "2026-11-17")

    def test_in_n_days(self):
        self.assertEqual(self.due("in 3 days"), "2026-11-05")

    def test_italian_weekday_and_time(self):
        self.assertEqual(self.due("martedì alle 8.00"),
                         "2026-11-03T08:00:00+01:00")

    def test_am_pm(self):
        self.assertEqual(self.due("Friday 5pm"), "2026-11-06T17:00:00+01:00")

    def test_unparseable_date_asks(self):
        with self.assertRaises(Clarification):
            self.due("sometime soon")


class TestActionCatalogue(ParserCase):
    """The action comes ONLY from actions_list.txt, matched deterministically;
    below 75% (or ambiguous) the user is asked to confirm - never guessed."""

    def test_catalogue_is_loaded_from_actions_list_file(self):
        names = {a["name"] for a in self.p.actions}
        self.assertEqual(names, {"add_card", "add_list", "update", "complete",
                                 "move_date", "archive", "query"})

    def test_exact_catalogue_name_matches(self):
        c = self.parse("action: add_card\ntitle: X")
        self.assertEqual(c.action, Action.add_card)

    def test_trigger_from_catalogue_matches(self):
        self.trello.cards = [{"id": "c1", "name": "Call supplier",
                              "closed": False}]
        c = self.parse("action: mark as done\ncard: Call supplier")
        self.assertEqual(c.action, Action.complete)
        self.assertEqual(c.target_card_id, "c1")

    def test_longest_trigger_wins_ties(self):
        # "move card" (update) beats "done" (complete): more specific phrase
        action, score, _ = self.p._match_action("move card to done")
        self.assertEqual(action, Action.update)
        self.assertEqual(score, 1.0)

    def test_below_75_percent_asks_the_user_to_confirm(self):
        with self.assertRaises(Clarification) as ctx:
            self.parse("action: balla")
        msg = str(ctx.exception)
        self.assertIn("add_card", msg)          # the catalogue is listed
        self.assertIn("Which one", msg)         # user must confirm, no guess

    def test_unknown_action_never_invented(self):
        with self.assertRaises(Clarification):
            self.parse("action: inventa qualcosa\ntitle: X")

    def test_no_action_line_means_chat_even_with_trigger_words(self):
        c = self.parse('aggiungi una scheda "Call supplier"')
        self.assertEqual(c.action, Action.chat)
        self.assertIn("aggiungi", c.reply)

    def test_italian_query_without_action_line_is_chat(self):
        c = self.parse("quali schede ho aperte?")
        self.assertEqual(c.action, Action.chat)


if __name__ == "__main__":
    unittest.main()

"""Unit tests for app/schema.py: Action + parse_action (legacy names),
Card validation rules (spec section 4), the CARD_FIELD_MAP conversions
(from_api / to_api / mirror_diff) and the Board / TrelloList mirrors."""
import unittest

from app.schema import (Action, Board, Card, TrelloList, WRITE_ACTIONS,
                        parse_action)


class TestParseAction(unittest.TestCase):
    def test_all_canonical_names_roundtrip(self):
        for a in Action:
            self.assertIs(parse_action(a.value), a)

    def test_action_instance_passthrough(self):
        self.assertIs(parse_action(Action.archive), Action.archive)

    def test_legacy_create_maps_to_add_card(self):
        self.assertIs(parse_action("create"), Action.add_card)

    def test_unknown_names_raise(self):
        with self.assertRaises(ValueError):
            parse_action("delete")
        with self.assertRaises(ValueError):
            parse_action(None)


class TestValidate(unittest.TestCase):
    def test_add_card_requires_title(self):
        with self.assertRaises(ValueError):
            Card(action=Action.add_card, title="").validate()

    def test_add_list_never_blocks_and_defaults_to_new_list(self):
        for title in ("", None):
            c = Card(action=Action.add_list, title=title)
            c.validate()
            self.assertEqual(c.title, "new List")

    def test_add_list_with_name_keeps_it(self):
        c = Card(action=Action.add_list, title="Blocked")
        c.validate()
        self.assertEqual(c.title, "Blocked")

    def test_modify_actions_require_target_card_id(self):
        for a in (Action.update, Action.complete, Action.move_date,
                  Action.archive):
            with self.assertRaises(ValueError):
                Card(action=a, title="x").validate()

    def test_move_date_requires_due(self):
        with self.assertRaises(ValueError):
            Card(action=Action.move_date, target_card_id="c1").validate()

    def test_update_requires_a_change(self):
        with self.assertRaises(ValueError):
            Card(action=Action.update, target_card_id="c1").validate()

    def test_update_accepts_each_single_change(self):
        changes = [dict(title="T"), dict(description="d"),
                   dict(due="2026-11-17T08:00:00+01:00"), dict(list_id="L2"),
                   dict(assignees=["m1"]), dict(done=True), dict(closed=True)]
        for ch in changes:
            Card(action=Action.update, target_card_id="c1", **ch).validate()

    def test_query_and_chat_need_nothing(self):
        Card(action=Action.query).validate()
        Card(action=Action.chat).validate()


class TestProperties(unittest.TestCase):
    def test_is_write_covers_exactly_the_write_actions(self):
        for a in Action:
            c = Card(action=a)
            self.assertEqual(c.is_write, a in WRITE_ACTIONS)

    def test_needs_clarification_boundary_is_0_7(self):
        self.assertFalse(Card(action=Action.chat, confidence=0.7)
                         .needs_clarification)
        self.assertTrue(Card(action=Action.chat, confidence=0.69)
                        .needs_clarification)

    def test_has_change(self):
        self.assertFalse(Card(action=Action.update, target_card_id="c1")
                         .has_change)
        for ch in (dict(title="T"), dict(description="d"), dict(due="x"),
                   dict(list_id="L"), dict(assignees=["m"]), dict(done=True),
                   dict(closed=True)):
            self.assertTrue(Card(action=Action.update, target_card_id="c1",
                                 **ch).has_change)


class TestFieldMap(unittest.TestCase):
    def test_map_covers_the_nine_trello_card_fields(self):
        self.assertEqual(Card.CARD_FIELD_MAP, {
            "target_card_id": "id", "title": "name", "description": "desc",
            "assignees": "idMembers", "due": "due", "done": "dueComplete",
            "closed": "closed", "list_id": "idList", "labels": "idLabels"})

    def test_to_api_maps_names_and_sends_only_carried_fields(self):
        c = Card(action=Action.update, target_card_id="c1", title="T",
                 description="d", assignees=["m1"], due="2026-11-17T08:00:00+01:00",
                 done=True, closed=True, list_id="L1", labels=["x"])
        self.assertEqual(c.to_api(), {
            "id": "c1", "name": "T", "desc": "d", "idMembers": ["m1"],
            "due": "2026-11-17T08:00:00+01:00", "dueComplete": True,
            "closed": True, "idList": "L1", "idLabels": ["x"]})

    def test_to_api_omits_none_empty_false(self):
        c = Card(action=Action.update, target_card_id="c1")
        self.assertEqual(c.to_api(), {"id": "c1"})

    def test_from_api_reverse_mapping(self):
        c = Card.from_api({"id": "c1", "name": "N", "desc": "d",
                           "idMembers": ["m1"], "due": "2026-11-17T08:00:00+01:00",
                           "dueComplete": True, "closed": False,
                           "idList": "L1", "idLabels": ["x"]})
        self.assertEqual(c.action, Action.query)   # reads are queries
        self.assertEqual(c.target_card_id, "c1")
        self.assertEqual(c.title, "N")
        self.assertEqual(c.description, "d")
        self.assertEqual(c.assignees, ["m1"])
        self.assertEqual(c.due, "2026-11-17T08:00:00+01:00")
        self.assertTrue(c.done)
        self.assertFalse(c.closed)
        self.assertEqual(c.list_id, "L1")
        self.assertEqual(c.labels, ["x"])

    def test_from_api_defaults_on_missing_fields(self):
        c = Card.from_api({})
        self.assertEqual(c.target_card_id, None)
        self.assertEqual(c.title, "")
        self.assertEqual(c.assignees, [])
        self.assertEqual(c.labels, [])
        self.assertFalse(c.done)
        self.assertFalse(c.closed)

    def test_to_api_from_api_roundtrip(self):
        c = Card(action=Action.update, target_card_id="c9", title="T",
                 due="2026-11-17T08:00:00+01:00", list_id="L2", done=True)
        back = Card.from_api(c.to_api())
        self.assertEqual(back.target_card_id, "c9")
        self.assertEqual(back.title, "T")
        self.assertEqual(back.due, "2026-11-17T08:00:00+01:00")
        self.assertEqual(back.list_id, "L2")
        self.assertTrue(back.done)


class TestMirrorDiff(unittest.TestCase):
    def test_identical_card_has_no_diff(self):
        c = Card(action=Action.add_card, title="T", description="d",
                 list_id="L1")
        self.assertEqual(c.mirror_diff(c.to_api()), {})

    def test_detects_title_and_due_disagreement(self):
        c = Card(action=Action.add_card, title="T", due="2026-11-17T08:00:00+01:00")
        diffs = c.mirror_diff({"name": "Other", "due": "2026-11-18T08:00:00+01:00"})
        self.assertEqual(diffs["title"], {"card": "T", "trello": "Other"})
        self.assertEqual(diffs["due"]["trello"], "2026-11-18T08:00:00+01:00")

    def test_none_card_fields_are_ignored(self):
        # None fields (due, list_id, ...) never produce a diff, even when
        # Trello reports a value for them
        c = Card(action=Action.add_card, title="T")
        self.assertEqual(c.mirror_diff({"name": "T",
                                        "due": "2026-11-17T08:00:00+01:00",
                                        "idList": "L1"}), {})


class TestBoardAndListMirrors(unittest.TestCase):
    def test_board_from_api(self):
        b = Board.from_api({"id": "b1", "name": "Team", "desc": "d",
                            "closed": True, "url": "u", "shortUrl": "su"})
        self.assertEqual((b.id, b.name, b.desc, b.closed, b.url, b.short_url),
                         ("b1", "Team", "d", True, "u", "su"))

    def test_list_from_api(self):
        l = TrelloList.from_api({"id": "L1", "name": "To-do", "closed": False,
                                 "idBoard": "b1", "pos": 16384.0,
                                 "subscribed": True})
        self.assertEqual((l.id, l.name, l.closed, l.id_board, l.pos,
                          l.subscribed),
                         ("L1", "To-do", False, "b1", 16384.0, True))


if __name__ == "__main__":
    unittest.main()

"""Offline tests for the real TrelloClient: exact HTTP requests it builds
(method, path, auth params, default-list fallback, lazy board-ID resolution,
create_list request). No network: urllib.request.urlopen is stubbed.
"""
import io
import json
import unittest
import urllib.error
from unittest import mock

from app.schema import Action, Card
from app.trello.client import TrelloClient


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def client():
    return TrelloClient("k", "t", "shortboard", default_list_id="Ldefault")


class StubHTTP:
    """Stands in for urllib.request.urlopen; records every request and
    replays the canned JSON responses in order."""

    def __init__(self, responses):
        self.responses = iter(responses or [{}])
        self.requests = []

    def __call__(self, req, *a, **kw):
        self.requests.append(req)
        return FakeResponse(next(self.responses))

    def __enter__(self):
        self._patch = mock.patch("urllib.request.urlopen", self)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        return False


def stub(*responses):
    return StubHTTP([{} if r is None else r for r in responses] or [{}])


def query(req):
    # full_url is "https://api.trello.com/1/cards?k=v&..."
    return req.full_url.split("?", 1)[1]


class TestCreateCard(unittest.TestCase):
    def test_create_falls_back_to_default_list(self):
        card = Card(action=Action.add_card, title="Call supplier",
                    description=None, due=None, assignees=["m1"], labels=[])
        with stub({"id": "c1"}) as up:
            client().create_card(card)
        req = up.requests[0]
        self.assertEqual(req.method, "POST")
        self.assertTrue(req.full_url.startswith(
            "https://api.trello.com/1/cards?"))
        q = query(req)
        self.assertIn("idList=Ldefault", q)
        self.assertIn("name=Call+supplier", q)
        self.assertIn("idMembers=m1", q)
        self.assertIn("key=k", q)
        self.assertIn("token=t", q)
        # empty optional fields are dropped, not sent as blanks
        self.assertNotIn("desc=", q)
        self.assertNotIn("due=", q)
        self.assertNotIn("idLabels=", q)

    def test_explicit_list_id_wins_over_default(self):
        card = Card(action=Action.add_card, title="X", list_id="Lnamed")
        with stub({"id": "c1"}) as up:
            client().create_card(card)
        q = query(up.requests[0])
        self.assertIn("idList=Lnamed", q)
        self.assertNotIn("idList=Ldefault", q)


class TestUpdateCard(unittest.TestCase):
    def test_update_sends_name_and_desc(self):
        card = Card(action=Action.update, target_card_id="card7",
                    title="New name", description="New desc")
        with stub({"id": "card7"}) as up:
            client().update_card(card)
        req = up.requests[0]
        self.assertEqual(req.method, "PUT")
        self.assertTrue(req.full_url.endswith("/cards/card7") or
                        req.full_url.startswith(
                            "https://api.trello.com/1/cards/card7?"))
        q = query(req)
        self.assertIn("name=New+name", q)
        self.assertIn("desc=New+desc", q)
        self.assertNotIn("due=", q)          # due None -> not sent

    def test_complete_sets_due_complete(self):
        card = Card(action=Action.complete, target_card_id="card7", due=None)
        with stub({}) as up:
            client().update_card(card)
        self.assertIn("dueComplete=true", query(up.requests[0]))

    def test_archive_sets_closed(self):
        card = Card(action=Action.archive, target_card_id="card7")
        with stub({}) as up:
            client().update_card(card)
        self.assertIn("closed=true", query(up.requests[0]))

    def test_move_and_assignees_are_forwarded(self):
        card = Card(action=Action.move_date, target_card_id="card7",
                    due="2026-11-01T10:00:00+01:00",
                    list_id="L2", assignees=["m1", "m2"])
        with stub({}) as up:
            client().update_card(card)
        q = query(up.requests[0])
        self.assertIn("due=2026-11-01T10", q)
        self.assertIn("idList=L2", q)
        self.assertIn("idMembers=m1%2Cm2", q)


class TestCreateList(unittest.TestCase):
    def test_create_list_posts_to_lists_with_board_and_name(self):
        c = client()
        responses = [{"id": "full24charboardid00001"},   # lazy board resolution
                     {"id": "Lnew1", "name": "Review"}]   # POST /lists result
        with stub(*responses) as up:
            new_list = c.create_list("Review")
        req = up.requests[1]
        self.assertEqual(req.method, "POST")
        self.assertIn("/lists", req.full_url)
        q = query(req)
        self.assertIn("idBoard=full24charboardid00001", q)
        self.assertIn("name=Review", q)
        self.assertIn("key=k", q)
        self.assertIn("token=t", q)
        self.assertEqual(new_list["id"], "Lnew1")

    def test_apply_routes_add_list_to_create_list(self):
        c = client()
        card = Card(action=Action.add_list, title="Blocked")
        responses = [{"id": "full24charboardid00001"}, {"id": "L9", "name": "Blocked"}]
        with stub(*responses) as up:
            result = c.apply(card)
        self.assertEqual(up.requests[1].method, "POST")
        self.assertIn("/lists", up.requests[1].full_url)
        self.assertIn("name=Blocked", query(up.requests[1]))
        self.assertEqual(result["id"], "L9")


class TestBoardIdResolution(unittest.TestCase):
    def test_board_cards_resolves_full_id_once_then_caches(self):
        c = client()
        responses = [{"id": "full24charboardid00001"},
                     [{"id": "c1", "name": "A"}],
                     [{"id": "c2", "name": "B"}]]
        with stub(*responses) as up:
            first = c.board_cards()
            second = c.board_cards()
        # request 1: GET /boards/shortboard?fields=id (lazy resolution)
        self.assertIn("/boards/shortboard", up.requests[0].full_url)
        self.assertIn("fields=id", query(up.requests[0]))
        # request 2: cards listed under the FULL id
        self.assertIn("/boards/full24charboardid00001/cards",
                      up.requests[1].full_url)
        # request 3: no re-resolution, full id reused
        self.assertIn("/boards/full24charboardid00001/cards",
                      up.requests[2].full_url)
        self.assertEqual(len(up.requests), 3)
        self.assertEqual(first[0]["name"], "A")
        self.assertEqual(second[0]["name"], "B")

    def test_search_filters_by_full_board_id(self):
        c = client()
        responses = [{"id": "full24charboardid00001"}, {"cards": [{"id": "c1"}]}]
        with stub(*responses) as up:
            found = c.search_cards("supplier")
        req = up.requests[1]
        self.assertIn("/search", req.full_url)
        q = query(req)
        self.assertIn("idBoards=full24charboardid00001", q)
        self.assertIn("modelTypes=cards", q)
        self.assertIn("query=supplier", q)
        self.assertEqual(found, [{"id": "c1"}])

    def test_get_card_and_members(self):
        c = client()
        responses = [{"id": "cardX"},          # get_card
                     {"id": "full24charboardid00001"},  # lazy board resolution
                     [{"id": "m1", "fullName": "Marco"}]]
        with stub(*responses) as up:
            card = c.get_card("cardX")
            members = c.list_members()
        self.assertIn("/cards/cardX", up.requests[0].full_url)
        self.assertEqual(card["id"], "cardX")
        self.assertIn("/boards/full24charboardid00001/members",
                      up.requests[2].full_url)
        self.assertEqual(members[0]["fullName"], "Marco")

    def test_list_lists_uses_full_board_id(self):
        c = client()
        responses = [{"id": "full24charboardid00001"},   # lazy resolution
                     [{"id": "Ltodo", "name": "To-do"},
                      {"id": "Ldoing", "name": "Doing"},
                      {"id": "Ldone", "name": "Done 🎉"}]]
        with stub(*responses) as up:
            lists = c.list_lists()
        req = up.requests[1]
        self.assertEqual(req.method, "GET")
        self.assertIn("/boards/full24charboardid00001/lists", req.full_url)
        q = query(req)
        self.assertIn("fields=id", q)
        self.assertIn("name", q)
        self.assertEqual([l["id"] for l in lists],
                         ["Ltodo", "Ldoing", "Ldone"])


class TestHTTPErrorLogging(unittest.TestCase):
    def test_error_body_is_logged_and_exception_reraised(self):
        # the real-world case: empty board id -> GET /boards//members -> 400
        err = urllib.error.HTTPError(
            "https://api.trello.com/1/boards//members", 400, "Bad Request",
            {}, io.BytesIO(b"invalid id"))

        def raising(req, *a, **kw):
            raise err

        with mock.patch("urllib.request.urlopen", raising):
            with self.assertLogs("app.trello.client", level="ERROR") as cm:
                with self.assertRaises(urllib.error.HTTPError):
                    TrelloClient("k", "t", "").list_members()
        self.assertIn("HTTP 400", cm.output[0])
        self.assertIn("invalid id", cm.output[0])   # Trello's reason
        self.assertIn("/boards//members", cm.output[0])
        self.assertNotIn("key=", cm.output[0])      # never log credentials

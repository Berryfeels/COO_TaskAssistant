# Trello_schema.md — mirroring structure schema

Reference for the structure of a Trello board as mirrored by the COO assistant
(`CODE/`). Trello is the single source of truth; the local SQLite `coo.db`
only mirrors what the bot itself created or handled.


- Local usage: `CODE/app/trello/client.py`, `CODE/app/schema.py`, `CODE/app/storage/db.py`

## 1. Hierarchy

```
Board 1 ──< List (column) 1 ──< Card
 │            │                  ├─ idLabels  ──> Label (color + name)
 │            │                  ├─ idMembers ──> Member (fullName, avatar…)
 │            │                  ├─ idChecklists ──> Checklist ──> CheckItem(s)
 │            │                  ├─ attachments (idAttachmentCover, previews…)
 │            │                  └─ badges (comments, checkItems, due, …) + actions (audit trail)
 ├─ labels   ├─ pos (order)
 ├─ members  └─ closed (archived)
 └─ prefs, url, shortUrl, labelNames
```

A **List is what the UI calls a column**. A **Card** is the bot's unit of work (the `Card` model in `schema.py`).

Board `actions` (the audit trail) are **not** nested in the board or card responses by default — fetch them via `GET /1/boards/{id}/actions`.

## 2. Board object

`GET /1/boards/{id}` (the board-tile component recommends
`?fields=name,prefs,url&structure=all&organization=true&organization_fields=displayName`).

| Field | Type | Notes |
| --- | --- | --- |
| `id` | string (24-char) | Full ID. The **short ID / URL slug** (e.g. `dQHqCohZ`) also works on most endpoints, but `/1/search?idBoards` and `POST /1/lists` need the full ID — the bot resolves it once via `GET /1/boards/{id}?fields=id` (`client.py: board_id`). |
| `name` | string | Board title |
| `desc` / `descData` | string | Board description |
| `closed` | bool | Board archived |
| `url` / `shortUrl` | string | `url` = `https://trello.com/b/<shortLink>/<board-slug>` (e.g. `…/b/lXq3WbXF/coo-assistant-agents-testing-sandbox`); `shortUrl` = `https://trello.com/b/<shortLink>` |
| `idOrganization`, `idMemberCreator` | string | |
| `prefs` | object | `permissionLevel`, `voting`, `comments`, `cardCovers`, `background`, … |
| `labelNames` | object | `{"green": "Ad", …}` — label colors that may have empty names |
| `lists`, `cards`, `labels`, `members`, `checklists`, `actions` | arrays | Nested only via the dedicated params: `?lists=all&cards=all&labels=all&members=all&checklists=all&actions=all`. **`structure=all` does NOT nest them** — it only adds a `structure` summary array (e.g. `[0,3,3,2,2]`). `labels` is not a valid `fields` value; use `labels=all` |

## 3. List object (a column)

| Field | Type | Notes |
| --- | --- | --- |
| `id` | string | This is the card's `idList` — the value the orchestrator resolves from a column NAME via `list_lists()` |
| `name` | string | e.g. "To-do", "Doing", "Done 🎉" |
| `closed` | bool | Archived column |
| `idBoard` | string | Owning board |
| `pos` | number | Position (order) on the board |
| `subscribed` | bool | |

Endpoints used:
- `GET /1/boards/{id}/lists?fields=id,name` — resolve column names → IDs (`client.py: list_lists`). Default returns **open** lists only; add `&filter=all` to also get archived columns
- `POST /1/lists?name=<name>&idBoard=<fullBoardId>` — **create a column** (`add_list`; `name` and `idBoard` are required query params)
- `PUT /1/lists/{id}` — rename / archive (`name`, `closed`, `pos`, `idBoard`)

## 4. Card object

`GET /1/cards/{id}` — the card component works directly on this response
(recommended fields: `name,closed,url,badges,idAttachmentCover,labels,…`).

| Field | Type | Bot meaning |
| --- | --- | --- |
| `id` | string | `target_card_id` |
| `idShort` | number | Human-friendly number shown in Trello UI |
| `name` | string | Card title = `Card.title` |
| `desc` / `descData` | string | Description = `Card.description` |
| `closed` | bool | Archived card = `Card.closed` (action `archive`) |
| `url` / `shortLink` | string | Card link |
| `idBoard` | string | |
| `idList` | string | Current column = `Card.list_id` (moving a card = updating `idList`) |
| `idMembers` / `idMembersVoted` | string[] | Assignees = `Card.assignees` |
| `idLabels` | string[] | Labels = `Card.labels` (always `[]` for now — label resolution not implemented) |
| `idChecklists`, `checkItemStates` | | Checklists; surfaced via `badges.checkItems / checkItemsChecked` |
| `due` | ISO 8601 string or null | Due date = `Card.due` |
| `dueComplete` | bool | Done flag = `Card.done` (action `complete`) |
| `pos` | number | Position inside the column |
| `badges` | object | `votes, comments, attachments, checkItems, checkItemsChecked, description, due, dueComplete, subscribed, viewingMemberVoted` |
| `dateLastActivity` | ISO 8601 | |
| `idAttachmentCover`, attachments | | Cover image |

Endpoints used by the bot (`CODE/app/trello/client.py`):

| Endpoint | Purpose |
| --- | --- |
| `POST /1/cards` (`idList` **required**, `name`, `desc`, `due`, `idMembers`, `idLabels`) | `add_card` → `create_card` |
| `GET /1/boards/{id}/cards?fields=id,name,desc,due,dueComplete,closed,idList,idMembers,idLabels` | `board_cards()` — resolve card IDs; deterministic, no index lag. Default returns **open** cards only; add `&filter=all` to also get archived cards |
| `GET /1/cards/{id}` | `get_card` |
| `PUT /1/cards/{id}` (`name`, `desc`, `due`, `dueComplete`, `closed`, `idList`, `idMembers`) | `update` / `complete` / `archive` / `move_date` → `update_card` |
| `GET /1/search?query=…&idBoards=<fullId>&modelTypes=cards` | `search_cards` — note: the search index lags >40 s behind, prefer `board_cards` |
| `GET /1/boards/{id}/members` | `list_members` — resolve assignee names → member IDs |

All calls are GET/POST/PUT with **query parameters only** plus `key` + `token`
(`client.py: _req`); empty values are dropped.

## 5. Component view (the two guide pages)

The guides are about embedding Trello UI via web components; they confirm the
data model above by showing exactly which API responses feed them:

- `<trello-board-tile>` — property `board` (object, the full board incl.
  `structure=all`), `footer` (bool, default true). No custom events.
- `<trello-card>` — property `card` (object, a full `GET /1/cards/{id}`
  response), `colorblind` (bool, default false), `labeltext` (bool, default
  true); custom event `labelTextChanged` with `event.detail.labeltext`.

## 6. Mirror mapping (Trello → local)

Trello stays the source of truth; the local mirror is written **only after**
the Trello call succeeded (`pipeline.py: _process`, `_record_list`).

| Trello | Local mirror | Where |
| --- | --- | --- |
| Card | `Card` dataclass (action + fields) | `CODE/app/schema.py`; `Card.from_api()` reverse-maps a card dict, `Card.to_api()` forward-maps, `mirror_diff()` audits drift |
| `POST /1/cards` write params | `Card` → query params | `client.py: create_card` |
| `PUT /1/cards/{id}` write params | `Card` → query params | `client.py: update_card` |
| List (column) created via `add_list` | row in `lists` table (`list_id`, `name`, `created_at`) | `db.py: list_save/list_get/list_all`; written by `pipeline._record_list` |
| Pending confirmation | row in `pending` table (`pending_id`, `user_id`, `card_json`, `created_at`, `reminders_sent`, `last_reminder_at`, `status`) | `db.py` (column renamed from `task_json` by a migration in `DB.__init__`); legacy action names are mapped by `parse_action()` (`schema.py`) |
| Handled message | row in `processed` (`message_id`, `processed_at`) — idempotency, never implies the action ran | `db.py: mark_processed/already_processed` |
| Incoming message | row in `queue` (FIFO per user, ingestion only) | `db.py: queue_push/queue_pop` |
| Board member | row in `users` (`simplex_id` ↔ `trello_member_id`) | `db.py`, gateway onboarding |

## 7. Constraints and quirks (design around these)

- `POST /1/cards` requires `idList` — fall back to the configured default list.
- Short board ID vs full 24-char ID: `/1/search` (`idBoards`) and `POST /1/lists`
  need the full ID; resolve lazily and cache (`client.py: board_id`).
- `/1/search` rejects an empty query (HTTP 400) and its index lags behind
  freshly created/renamed cards — use `GET /1/boards/{id}/cards` for reads.
- `GET /1/boards/{id}/cards` and `GET /1/boards/{id}/lists` return only
  **open** (non-archived) items by default — add `filter=all` to include
  archived cards/columns.
- `structure=all` on `GET /1/boards/{id}` does not embed the collections;
  it only adds a `structure` counts array. Use `lists=all&cards=all&…` to nest.
- A List is created with `POST /1/lists` (`name` + `idBoard` required); there is
  no "default column" for lists — `add_list` without a name defaults to
  `"new List"` locally (`schema.py: Card.validate`).
- Auth: `key` + `token` as query params on every request (OAuth 1.0a style);
  REST scopes e.g. `read:board:trello`, `write:board:trello`.

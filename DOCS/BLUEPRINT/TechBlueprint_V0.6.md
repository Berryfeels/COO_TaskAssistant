# COO Task Management Tool — Revised Specification v3 — Tech Blueprint V6

Spec plus architecture, Trello integration details, tool list and project skeleton.

**Changes in V6:**
- **Trello naming everywhere.** The local `Task` model is now the **`Card`** model (`app/schema.py`), and the board structure is mirrored explicitly: `Board 1—< TrelloList (column) 1—< Card`. The action `create` is renamed **`add_card`** (symmetric with `add_list`); the Trello API field names live in `Card.CARD_FIELD_MAP` (`from_api` / `to_api` convert). The Trello object model itself is documented in **`DOCS/Trello_schema.md`**, which this blueprint references.
- **`add_list` never blocks:** a column is always created; without a name it defaults to `"new List"` (`Card.validate`). `add_card` still requires a title.
- **No confirmation loop (decision 2026-10-29).** Write actions are applied to Trello **immediately**; the bot only confirms the outcome. The `pending` table remains solely to drain pendings created before the change (reminders/expiry still run). Trello is written FIRST, the local DB only AFTER the write succeeded.
- **Free conversation owned by the LLM.** Greetings/small talk are answered directly (`action: chat`); the system prompt owns the conversation and card content stays English.
- **Legacy compatibility.** `parse_action()` maps the old `"create"` action name (stored pending rows, occasional LLM output) to `add_card`; the DB column `task_json` is migrated to `card_json` in place on startup (`DB.__init__`).
- The SimpleX WebSocket gateway is the **live message entry point** (`SimpleXGateway.on_chat_event` → `Pipeline.handle_incoming`); the same call the tests make is made by real user messages.

<details open>
<summary><strong>Index</strong> — click a section to jump to it; collapse this block to hide it</summary>

- [1. Description](#1-description)
- [2. Architecture and agent list](#2-architecture-and-agent-list)
  - [2.1 Components](#21-components)
  - [2.2 Flow](#22-flow)
  - [2.3 Structure](#23-structure)
- [3. List of event triggers](#3-list-of-event-triggers)
- [4. Card object (mirrors a Trello card)](#4-card-object-mirrors-a-trello-card)
- [5. Writes, pending drain and idempotency](#5-writes-pending-drain-and-idempotency)
  - [5.1 Pending lifecycle (legacy drain)](#51-pending-lifecycle-legacy-drain)
  - [5.2 Idempotency and mark_processed semantics](#52-idempotency-and-mark_processed-semantics)
  - [5.3 Concurrency: FIFO](#53-concurrency-fifo)
  - [5.4 Near-duplicate guard (reworded repeats)](#54-near-duplicate-guard-reworded-repeats)
- [6. Trello integration: can everything go through the API?](#6-trello-integration-can-everything-go-through-the-api)
  - [6.1 Authentication](#61-authentication)
  - [6.2 Editing cards and columns](#62-editing-cards-and-columns)
  - [6.3 Incoming events (Trello to the system): webhooks](#63-incoming-events-trello-to-the-system-webhooks)
  - [6.4 Limits to design around](#64-limits-to-design-around)
- [7. Tech list](#7-tech-list)
- [8. Requirements (decisions)](#8-requirements-decisions)
- [9. Project skeleton](#9-project-skeleton)
  - [9.1 Core data flow in code (outline)](#91-core-data-flow-in-code-outline)
  - [9.2 Suggested build order](#92-suggested-build-order)
- [10. Run and clean shutdown](#10-run-and-clean-shutdown)
  - [10.1 Start](#101-start)
  - [10.2 Clean shutdown](#102-clean-shutdown)

</details>

## 1. Description

The agent automates team board management on Trello. The worker interacts with a bot as his/her assistant. Through text input (SimpleX), the user manages the board; the bot may also hold free conversation.

**Features:** add a card; add a column (`add_list`); edit a card (archive, set as done, move date, move column, modify); monitor deadlines; notify the worker; answer chat.

**Design principles.** Only use an LLM where language understanding is needed; everything else is deterministic code. Trello´s board is the single source of truth for cards and columns commands and queries: every Trello write happens FIRST and the local DB is updated only AFTER the write succeeded. Since 2026-10-29 there is **no confirmation loop** — writes are applied immediately and the bot only confirms the outcome. Every event is processed exactly once.

## 2. Architecture and agent list

### 2.1 Components

| Original component | Revised | Type | Reason |
| --- | --- | --- | --- |
| Director Agent | Orchestrator | LLM | Decides intent (add_card / update / complete / move_date / archive / add_list / query / chat) and calls Trello read tools to resolve which card or column is meant. |
| Subagent 1: Notifier (SimpleX) | SimpleX Gateway | Code | Pure I/O: receives user messages, sends notifications. No reasoning needed. The live entry point is `on_chat_event` → `Pipeline.handle_incoming`. |
| Subagent 2: Transcriptor | Transcriber | Code | Whisper runs only if the input is audio. Transcript is passed directly to the next step. (Text-only in the live path so far.) |
| Subagent 3: Card definer | Merged into Orchestrator | LLM | Extraction and intent are one reasoning step. Output is a structured `Card` object (JSON). |
| Subagent 4: Dispatcher | Queue / event bus | Code | Routing does not need an LLM. A SQLite queue gives retries and idempotency (FIFO per user, ingestion only). |
| Subagent 5: Trello Manager | Trello Service | Code | API wrapper (`TrelloClient`). Writes first, local mirror after. |
| (missing) | Scheduler | Code | Trello does not push deadline-approaching events, so reminders must be generated by us (hourly job; also drains legacy pendings). |
| (missing) | Confirmation loop | Code | **Removed 2026-10-29**: writes apply immediately. The pending-matching code remains only to drain pendings created before the change. Warns about similar pending/existing cards (near-duplicate guard, 5.4). |

### 2.2 Flow

- **New message.** A SimpleX chat event goes straight to the Orchestrator as text (`handle_incoming`); no kDrive archive in the live path.
- **The Orchestrator reads existing state.** "Move the supplier call to Friday" only works if the agent can search open Trello cards and resolve the right one. It needs read tools, not just a form to fill (`board_cards` is preferred over `/1/search`: deterministic, no index lag).
- **Write step.** The pipeline validates the `Card`, applies it to Trello immediately (`trello.apply`), then mirrors the result locally (`lists` table for `add_list`, `processed` for the message ID) and replies with the outcome, e.g. "Done: 'Call supplier' created in Trello." Legacy confirmations ("OK <pending_id>") still apply their pending action first.
- **Archive instead of delete.** Deletion is implemented as archiving the card (`closed=true`, recoverable).

### 2.3 Structure

SimpleX Gateway (in/out) — the real message entry point
-> Pipeline: FIFO queue (SQLite) -> idempotency -> legacy confirm-match -> Orchestrator LLM (intent + extraction, with Trello read tools)
-> write actions: validate -> Trello write FIRST -> local DB AFTER -> outcome reply
-> query/chat: answered immediately (queries read Trello only, never the local DB)

Scheduler (hourly: pending reminders + expiry) ---> Notification builder --> SimpleX Gateway

Shared: SQLite DB (user mapping, processed IDs, pending drain, FIFO queue, lists mirror)

## 3. List of event triggers

| Trigger | Original meaning | Implemented as |
| --- | --- | --- |
| 1 | New recording | SimpleX gateway receives a text message (`newChatItems` event; audio transcription optional) |
| 2 | New transcription in folder | Transcriber hands the text to the Orchestrator in code; kDrive file is an archive only |
| 3 | Identification of cards complete | Orchestrator returns a valid `Card` object, then the pipeline writes to Trello immediately and confirms the outcome |
| 4 | In events on Trello (commands from outside) | Trello webhook: card created, updated, moved, completed, commented, member added by humans |
| 5 | Out events on Trello (reminder notification) | Scheduler job. Trello does not send an approaching-deadline event to our endpoint |

## 4. Card object (mirrors a Trello card)

Schema returned by the Orchestrator as structured JSON; parsed into the `Card` dataclass in `app/schema.py`. The Trello side of the mapping (board/list/card objects, endpoints, component views) is documented in **`DOCS/Trello_schema.md`**; `Card.CARD_FIELD_MAP` holds the field-level mapping and `Card.from_api()` / `Card.to_api()` convert both ways. `Board` and `TrelloList` dataclasses mirror the other two Trello objects.

| Field | Type | Trello card field (via `CARD_FIELD_MAP`) | Notes |
| --- | --- | --- | --- |
| action | enum | - | add_card, update, complete, move_date, archive, add_list, query, chat. Legacy `"create"` is mapped to `add_card` by `parse_action()` |
| target_card_id | string or null | id | Null for add_card / add_list. Resolved via `board_cards` for other actions |
| title | string | name | Short imperative, in **English**. Required for add_card; for add_list it is the **column name** and defaults to `"new List"` when empty |
| description | string | desc | Context from the original message, in **English** (every word translated): constraints, contact person with email/phone, location, notes |
| assignees | list of member IDs | idMembers | The responsible person: in "X must do Y" the subject X is the responsible member, resolved against the board members by name; unknown person → clarifying question |
| due | ISO 8601 datetime | due | Date AND time when given, with the explicit **Europe/Berlin** UTC offset (e.g. 2026-11-17T08:00:00+01:00) |
| done | bool | dueComplete | Set by action complete |
| closed | bool | closed | Archived card (action archive) |
| list_id | string | idList | e.g. the resolved ID of To-do / Doing / Done |
| labels | list | idLabels | Always `[]` for now: label resolution is not implemented, never invent label IDs |
| confidence | 0-1 | - | Below 0.7 the bot asks a clarifying question |
| reply | string | - | Free-text answer, used only for action chat |
| source_message_id | string | - | For idempotency and audit |

## 5. Writes, pending drain and idempotency

Since 2026-10-29 every write action is applied to Trello **immediately** (no user confirmation); the bot only reports the outcome. The `pending` table is kept to drain actions created before that date.

### 5.1 Pending lifecycle (legacy drain)

1. **Creation (legacy).** Pending rows created before 2026-10-29 carry `pending_id`, `user_id`, `card_json` (column migrated from `task_json`), `created_at`.
2. **Confirmation window.** 24 hours. Unconfirmed pending actions expire after this window.
3. **Reminders.** While a pending action is awaiting confirmation, the Scheduler sends up to **3 reminders every 7 hours**. Reminders are only sent during daytime hours: **no reminder is ever sent between 02:00 and 05:00 Europe/Berlin**; if a reminder slot falls in that window it is deferred to 05:00.
4. **Expiry.** After 24 hours the pending action is **cancelled** and the user receives a notification.
5. **Confirmation.** A user reply confirming the pending_id applies it to Trello (Trello write FIRST, then `pending` status = applied, then the message is marked processed).

### 5.2 Idempotency and mark_processed semantics

`mark_processed(msg.id)` means: *this incoming message has been handled and must never be processed again*. It is **not** a confirmation and does **not** imply the action was executed.

- For write actions, `mark_processed` is called only **after** the Trello write succeeded and the local mirror was updated. If the process crashes before the Trello write, the message is safely re-processed on retry (the write is then attempted again; Trello-side duplicates are surfaced by the near-duplicate guard, 5.4).
- For queries, `mark_processed` is called after the reply is sent, so a crash keeps the message retryable.
- A duplicate confirmation (same text twice) is ignored via the processed IDs.

### 5.3 Concurrency: FIFO

- **FIFO applies to ingestion only.** Incoming messages per user are interpreted strictly in arrival order (single FIFO queue per user in the `queue` table).
- **Each message is an independent process.** One message never blocks the next: every message is interpreted on its own, with the per-user conversation history (last 12 turns) as context.
- **Legacy confirmations are bound to a pending_id.** A reply confirming a reference applies that specific action. A bare "OK"/"YES" with exactly one active pending confirms it; with several active pendings the bot lists them and asks which one is meant (never guesses).
- Back-to-back messages are handled by the same FIFO: each is interpreted in order; a message intended as a confirmation is matched against a pending_id before being treated as a new instruction.

### 5.4 Near-duplicate guard (reworded repeats)

Message-ID idempotency (5.2) cannot catch a card the user forgot about and prompts again with different wording. The guard makes the user aware in the outcome message.

- **Scope.** Only `add_card` actions. On every add_card, the pipeline compares the new title against (a) the user's active pending add_card actions and (b) every open card on the board.
- **Method.** Deterministic, no LLM: titles are normalized (casefold, punctuation stripped, whitespace collapsed) and compared with a sequence-similarity ratio; a ratio >= 0.75 counts as a near-duplicate.
- **Behavior.** Hits are appended to the confirmation message as a heads-up, e.g. *"Heads-up: similar card(s) found: 'Prepare dairy-free cake' (already on the board). Check whether this is a duplicate."* The guard never blocks and never writes.
- **Limits.** Title similarity only: heavily reworded paraphrases with different vocabulary may fall under the threshold. Semantic dedupe (embeddings or LLM-side matching) is a possible later extension.

## 6. Trello integration: can everything go through the API?

The full Trello object model (board / list / card fields, endpoints, component views, mirror mapping, quirks) is in **`DOCS/Trello_schema.md`**. Summary:

### 6.1 Authentication

Create an API key (Power-Up admin) and generate a token with read/write scope. Use a dedicated "Task Bot" Trello account so bot actions can be told apart from human ones. Every request carries `key` + `token` as query parameters (`TrelloClient._req`); empty values are dropped.

### 6.2 Editing cards and columns

| Operation | Action | Endpoint | Key parameters |
| --- | --- | --- | --- |
| Add card | add_card | POST /1/cards | idList (**required**; falls back to the configured default list), name, desc, due, idMembers, idLabels |
| Modify / move date / set done / move column | update, move_date, complete | PUT /1/cards/{id} | name, desc, due, dueComplete, idList, idMembers |
| Archive (recommended) | archive | PUT /1/cards/{id} | closed=true (recoverable) |
| Permanent delete | - | DELETE /1/cards/{id} | Irreversible, avoid |
| Add column | add_list | POST /1/lists | name (defaults to "new List"), idBoard (**full 24-char ID**) |
| Read cards | query / tools | GET /1/boards/{id}/cards?fields=id,name,desc,due,dueComplete,closed,idList,idMembers,idLabels | Preferred: deterministic, no index lag |
| Search | tools | GET /1/search?query=…&idBoards=<fullId>&modelTypes=cards | Index lags >40 s; rejects empty query (HTTP 400) |
| Columns | tools | GET /1/boards/{id}/lists?fields=id,name | Resolves a column NAME to its idList |
| Members | tools | GET /1/boards/{id}/members | Resolves assignee names to member IDs |
| Board ID | - | GET /1/boards/{id}?fields=id | Short slug → full 24-char ID, resolved once and cached (`TrelloClient.board_id`) |

### 6.3 Incoming events (Trello to the system): webhooks

- Register with POST /1/webhooks giving idModel (board, list or card) and a public HTTPS callbackURL.
- Trello posts a JSON action for each change: card created, updated (due date, list move, completion), commented, member added.
- The endpoint must answer Trello's initial HEAD request and should verify the HMAC-SHA1 signature header on every call.
- Webhooks can be disabled after repeated delivery failures: add a health check that re-registers them, plus a periodic heartbeat verifying the webhook is still active.
- Webhooks also fire for the bot's own edits. Ignore events whose actor is the bot's member ID to avoid loops.
- Human edits made directly in Trello arrive through the same webhook and become notifications to the people involved.

### 6.4 Limits to design around

- **Reminders.** There is no webhook event for an approaching deadline. Trello's own due reminders go to Trello's channels (app, email), not to our endpoint. The Scheduler sends reminders itself through SimpleX.
- **Outgoing messages.** Trello has no chat API. All user notifications go through SimpleX, built by our Notification builder.
- **Rate limits.** Roughly 300 requests per 10 s per API key and 100 per 10 s per token. Ample for a team, but prefer webhooks over polling.
- **Board ID forms.** Most endpoints accept the short slug, but `/1/search` (`idBoards`) and `POST /1/lists` need the full 24-char ID — resolve lazily and cache.

## 7. Tech list

| Layer | Options (as implemented) |
| --- | --- |
| Chat interface | SimpleX Chat CLI in WebSocket API mode + Python client (`app/gateway/simplex.py`) |
| Audio | ffmpeg + Whisper (optional; live path is text) |
| LLM | Mistral API chat completions with function calling, stdlib only (`app/orchestrator/mistral_llm.py`); bounded tool loop (5), per-user history (12 turns), JSON extracted from the reply |
| Orchestration | Plain Python, no framework (`app/pipeline.py`) |
| Webhook endpoint | (not yet live) FastAPI/Node behind public HTTPS |
| Queue / state | SQLite (`app/storage/db.py`): tables `users`, `processed`, `pending` (payload column `card_json`, migrated from `task_json`), `queue`, `lists` (mirror of columns created via add_list) |
| Scheduler | Hourly asyncio loop in `scripts/run_bot.py` (pending reminders + expiry) |
| Storage | Local SQLite (`coo.db`); kDrive archive optional |
| Deployment | Single process + simplex-chat CLI; secrets in `CODE/.env`; structured logging to stderr |
| Card backend | Trello REST API (`app/trello/client.py`) |

## 8. Requirements (decisions)

| Requirement | Decision |
| --- | --- |
| User mapping table | SimpleX contact ↔ Trello member (`users` table), filled by the onboarding gate. If a named assignee cannot be resolved, the bot asks a clarifying question. |
| Date handling | Relative dates ("next Tuesday") resolved in **Europe/Berlin**. |
| Languages | Input in **any language**; the bot answers in the user's language. **Card content is always English** (title, description). |
| Naming | Code speaks Trello: `Card` / `Board` / `TrelloList`, actions `add_card` / `add_list`. Legacy `"create"` accepted via `parse_action()`. |
| Idempotency | The same message must never be processed twice (`processed` table, see 5.2). A reworded repeat of a forgotten card is flagged by the near-duplicate guard (5.4), never silently duplicated. |
| Confirmation | **None since 2026-10-29**: writes apply immediately, the bot confirms the outcome. Legacy pendings still expire after 24 h with 3 daytime reminders (never 02:00–05:00 Berlin). |
| Concurrency | FIFO for ingestion only; every message is an independent process; legacy confirmations bound to pending_id, ambiguous "OK" with multiple active pendings triggers a clarification. |
| Error handling and retries | Trello write failure → the message is marked processed, nothing was written, the user gets a clear error and can resend. LLM/API failure → error reply, nothing written. Gateway logs pipeline errors and keeps consuming events. |
| Permissions | Onboarding gate admits only known contacts mapped to Trello members. |
| Privacy / GDPR | Retention of transcripts, where they are stored, who can read them. |
| Ambiguity policy | Several matching cards or unknown assignee → confidence < 0.7 plus ONE clarifying question. |
| Trello board design | Lists = columns, resolved by name via `list_lists()`; `DOCS/Trello_schema.md` is the structure reference. |

## 9. Project skeleton

Actual repository layout (Python, stdlib only):

```text
CODE/
  README.md  pyproject.toml  .env
  app/
    config.py             # Settings from env (.env)
    schema.py             # Board / TrelloList / Card models + Action enum + parse_action (section 4)
    pipeline.py           # Pipeline: FIFO ingestion, idempotency, legacy confirm-match, interpret, immediate writes, scheduler jobs
    similarity.py         # near-duplicate title guard (spec 5.4)
    dates.py              # relative-date resolution, quiet hours (Europe/Berlin)
    gateway/
      simplex.py          # WebSocket client: the REAL message entry point (on_chat_event -> handle_incoming), reconnect loop, send
      onboarding.py       # admit gate: SimpleX contact <-> Trello member
    orchestrator/
      agent.py            # LLMProvider protocol
      mistral_llm.py      # Mistral chat completions + tools (board_cards, search_cards, get_card, list_members, list_lists), _parse -> Card
    confirm/
      pending.py          # legacy pending lifecycle: match_confirmation, reminders, expiry
    trello/
      client.py           # TrelloClient: _req, board_id resolution, create_card/update_card/create_list, apply, reads
    notify/
      builder.py          # render_* user-facing texts (English)
    storage/
      db.py               # SQLite: users, processed, pending (card_json), queue, lists + task_json->card_json migration
  tests/                  # 68 unit tests (schema, dates, builder, trello client, similarity, gateway)
  scripts/
    run_bot.py            # wire real components and run (gateway + hourly scheduler)
    trello_sandbox_test.py / italian_scenario_test.py / mistral_conversation_test.py
DOCS/
  Trello_schema.md        # Trello board/list/card structure + mirror mapping (reference)
  BLUEPRINT/TechBlueprint_V*.md
```

## 9.1 Core data flow in code (outline)

```python
async def handle_incoming(user_id, message_id, text):   # gateway entry point
    db.queue_push(user_id, message_id, text)            # FIFO per user
    await drain(user_id)

async def _process(user_id, message_id, text):
    if db.already_processed(message_id):                # idempotency (5.2)
        return
    decision = pending.match_confirmation(user_id, text)   # legacy pendings only
    if decision.kind == "confirm":
        result = trello.apply(decision.card)            # Trello FIRST
        record_list(decision.card, result)              # DB AFTER
        pending.apply_by_id(decision.pending_id)
        db.mark_processed(message_id)
        return gateway.send(user_id, render_result(decision.card, result))

    card = llm.interpret(text, {"source_message_id": message_id, "user_id": user_id}, trello)
    if card.action == Action.chat:                      # free conversation
        db.mark_processed(message_id)
        return gateway.send(user_id, render_chat(card))
    if card.action == Action.query:                     # read-only: Trello only
        await answer_query(user_id, card)               # board_cards + list_lists
        db.mark_processed(message_id)
        return

    card.validate()                                     # add_card needs title; add_list defaults "new List"
    similar = similar_cards(user_id, card)              # near-duplicate guard (add_card only, 5.4)
    result = trello.apply(card)                         # Trello FIRST (source of truth)
    record_list(card, result)                           # lists table AFTER (add_list)
    db.mark_processed(message_id)                       # DB AFTER the write
    gateway.send(user_id, render_result(card, result, similar))

async def reminder_job():                               # hourly, legacy pendings
    for row in pending.due_for_reminder(now):
        if pending.quiet_hours(): continue              # never 02:00-05:00 Berlin
        gateway.send(row.user_id, render_reminder(...))

async def expiry_job():                                 # hourly, legacy pendings
    for pending_id, user_id, card in pending.expire(now):
        gateway.send(user_id, render_cancelled(pending_id, card))
```

## 9.2 Suggested build order

1. Trello service and Card schema, tested against a sandbox board. **(done)**
2. Orchestrator with text input, card/column resolution via tools. **(done)**
3. SimpleX gateway as the real message entry, FIFO queue. **(done)**
4. Immediate writes + outcome confirmation (no confirmation loop). **(done)**
5. Legacy pending drain: reminders, 24 h expiry, cancellation notice. **(done)**
6. Whisper transcription for audio.
7. Webhook receiver, loop prevention and notifications for human edits.
8. kDrive archive, GDPR retention rules, deployment and monitoring.

## 10. Run and clean shutdown

### 10.1 Start

1. Start the SimpleX Chat CLI with the WebSocket API enabled (one process, keeps running):

   ```bash
   simplex-chat -d ~/.simplex/coo-bot.db -p 5225 --user-display-name "COO Assistant"
   ```

   `-p 5225` is the chat server port the bot connects to; `-d` is the CLI database prefix.

2. With `CODE/.env` configured (Trello, Mistral, `SIMPLEX_WS_URL`), start the bot:

   ```bash
   cd CODE
   python3 scripts/run_bot.py            # logs to stderr; use 2>&1 | tee bot.log to persist
   ```

The bot connects to `SIMPLEX_WS_URL` (default `ws://127.0.0.1:5225`), restores persisted state from `DATABASE_PATH` (default `coo.db`: onboarded contacts, processed message IDs, pending actions, lists mirror), runs the `task_json → card_json` column migration in place on old DBs, and runs an hourly loop for pending reminders and 24 h expiry. **Run exactly one bot instance**: the CLI delivers each chat event to one client only, so a second instance (or a debug listener) steals events. On restart, kill the old PID first.

### 10.2 Clean shutdown

1. **Stop the bot first**: send SIGINT (Ctrl+C in its terminal, or `kill <pid>`). `scripts/run_bot.py` catches `KeyboardInterrupt`, prints "COO assistant stopped." and exits with code 0. All state is committed to SQLite at each step, so stopping at any point loses nothing — on the next start the bot resumes from the persisted DB.
2. **Then stop `simplex-chat`** (Ctrl+C or `pkill -f simplex-chat`).

Order matters: stop the bot before the SimpleX CLI, so no incoming message is left half-processed between gateway and pipeline.

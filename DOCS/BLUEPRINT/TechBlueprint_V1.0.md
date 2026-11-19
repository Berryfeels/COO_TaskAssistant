# COO Task Management Tool — Technical Blueprint (V6, cleaned draft)

## 1. Description

A digital COO executive assistant. It engages with the team's Trello board automatically and creates tasks on behalf of the COO after meetings. It supports the COO and each team member in completing tasks through chat answers, reminders and notifications.

**Features:** answer chat; create a card; create a list; update a card; monitor deadlines; notification system.

### Design principles

- **Orchestrator (LLM) takes only text.** Audio is transcribed before it reaches the LLM.
- **Language.** The user can write in any language and the bot answers in the user's language. Text written to Trello (titles, descriptions, comments) is always English.
- **Trello is the single source of truth.** Every Trello write happens first; the local DB is updated only after the write succeeded.
- **Write confirmation comes from the Trello API response**, not from a webhook. Webhooks (once live) are used for human edits made directly in Trello and, optionally, for cache synchronization.
- **Naming mirrors Trello.** Software objects and functions use Trello's vocabulary: card, list, member, label, due, checklist, comment.
- **Time.** All dates and times are interpreted in `Europe/Berlin` (CET/CEST, including daylight saving). Trello stores dates in UTC: convert at the boundaries (LLM/user side ↔ Trello side).

## 2. Architecture

### 2.1 Flow

1. **Message in.** A SimpleX chat event arrives. If it is audio, it is transcribed to text.
2. **Pipeline.** FIFO queue → idempotency check → legacy confirmation match → Orchestrator LLM (intent + extraction).
3. **Orchestrator output.** The LLM answers in **plain words, never JSON**: for a board action, one labelled line per field (`action:`, `title:`, `card:`, `assign:`, `due:`, `list:`, `note:`, `question:`) using names *as the user said them* (list name, member name, card name); for pure conversation, free prose with no `action:` line. Function calling is used only for the read tools (`board_cards`, `search_cards`, `get_card`, `list_members`, `list_lists`). The LLM never handles Trello IDs.
4. **Validation and resolution (deterministic code).** The parser fills the `Card` JSON from the LLM's words, matches the `action:` line against the catalogue `./actions_list.txt` (≥ 75% similarity selects the action; below, or two equally close, ONE clarifying question to the user — see 2.3), validates against the schema and the board state, resolves names to IDs with fuzzy matching using the cached board structure (2.4), and returns clear errors to the LLM when something does not match.
5. **Write step.** Deterministic code builds the Trello API call and executes it. The API response confirms the outcome.
6. **After success.** The local DB is updated, `mark_processed` is called, and the bot sends a confirmation message to the user (e.g. "Done: 'Call supplier' card has been created.").
7. **Monitoring.** A scheduler regularly checks upcoming due dates and notifies the people involved through SimpleX.

### 2.2 Components

- **SimpleX Gateway (in/out):** the real message entry point.
- **LLM Agent:** answers in plain words. For a board action it picks one action from the catalogue `./actions_list.txt` (loaded at runtime, embedded in the system prompt, matched deterministically by the parser — see 2.3 and 4.1) and writes one labelled line per field with names as the user said them. Catalogue names and descriptions are written in human terms; the description is where "human wording ↔ Trello action" lives. The read tools (`board_cards`, `list_members`, `list_lists`, `search_cards`, `get_card`) remain function-calling tools.
- **Validation layer (deterministic):** schema validation, name → ID resolution, board-state checks, rate limiting and retries.
- **Scheduler:** deadline reminders (see 4.4).
- **Notification builder:** composes all outgoing SimpleX messages.
- **Shared SQLite DB:** user mapping, processed message IDs, pending actions, FIFO queue, reminders sent, per-user conversation history, board structure cache (lists, members, labels with their IDs; see 2.4).

Example of a catalogue entry (`add_card` in `./actions_list.txt`):

```json
{
  "name": "add_card",
  "description": "Create a new task. Use when the user asks to add, create, note down, or remind about something to do.",
  "input_schema": {
    "properties": {
      "title": {"type": "string"},
      "list": {"type": "string", "description": "List name as the user said it. Omit if not stated."},
      "members": {"type": "array", "items": {"type": "string"}, "description": "People assigned, as named by the user."},
      "due": {"type": "string", "description": "ISO 8601 datetime, resolved from the current date given in the prompt."},
      "labels": {"type": "array", "items": {"type": "string"}},
      "description": {"type": "string"}
    },
    "required": ["title"]
  }
}
```

### 2.3 JSON validation (deterministic)

- The parser fills the JSON (the `Card`) from the LLM's plain words and validates it against the action catalogue and the tool schema — the LLM never emits JSON.
- Resolve names to IDs (members, lists, labels) from the cached board structure (2.4), with fuzzy matching.
- Validate against the board state: does this list exist? is this member on the board?
- Return clear errors to the LLM so it can self-correct in one retry, e.g. `no list called 'Doing'; available: To Do, In Progress, Done`.
- Respect rate limits and retry transient failures.

**The supported actions.** Exactly the seven in the catalogue `./actions_list.txt` (loaded at runtime, see 4.1). The LLM may choose only among these — it never invents an action:

| Action | Does | Key fields |
| --- | --- | --- |
| `add_card` | Create a new card on the board (`POST /cards`) | `title` (required), `list`, `members`, `due`, `labels`, `description` |
| `add_list` | Create a new column on the board (`POST /lists`) | `title` (the column name) |
| `update` | Modify an existing card (`PUT /cards/{id}`): title, notes, assignees, due, or move it to another column | `card` (required); then only the fields to change |
| `complete` | Mark a card as done: `dueComplete=true` and move it to the Done column | `card` (required) |
| `move_date` | Change only the due date of an existing card | `card` (required), `due` (required) |
| `archive` | Archive (close) a card via `PUT closed=true` — not delete | `card` (required) |
| `query` | Read-only: list the open cards on the board | — |

Free conversation (`chat`) is not an action: it is the *absence* of an action in the LLM's answer.

**Action matching is deterministic.** The action named by the LLM is matched against the catalogue (name, description, trigger phrases; similarity ratio): a match of **>= 75%** selects that action; below 75%, or two actions equally close, the bot asks the user ONE clarifying question listing the catalogue entries — it never guesses. The parser then fills the `Card` JSON from the LLM's words and resolves names to IDs as above.

### 2.4 Board structure cache (IDs of lists, members, labels)

The bot keeps a small cache of the board structure: lists, members and labels with their Trello IDs (plus the full 24-char board ID, see 4.4). It is used only to resolve names to IDs and to validate against the board state. **Cards are not cached**: they are always read from Trello when needed (duplicate check, card lookup, scheduler).

The cache is filled with a single request, e.g. `GET /1/boards/{id}?lists=open&members=all&labels=all`, and simply overwritten on refresh (no diff). Three refresh strategies, combined:

1. **TTL:** if the cache is older than 10-30 minutes, it is re-read at the next message.
2. **Refresh on miss:** if a named list, member or label is not found, the cache is re-read once and the lookup retried before an error is returned.
3. **Webhook invalidation** (once webhooks are live): events such as `createList`, `updateList`, `addMemberToBoard`, `createLabel`, `deleteLabel` trigger an immediate re-read.

A full mirror of the cards in SQLite (initial sync, webhook events, periodic resync) is a possible later extension if the scheduler or the duplicate guard become slow or hit rate limits. It depends on the webhook endpoint being live.

## 3. Message management

### 3.1 Idempotency and `mark_processed` semantics

`mark_processed(msg.id)` means: *this incoming message has been handled and must never be processed again*. It is **not** a confirmation and does **not** imply the action was executed.

- For write actions, `mark_processed` is called only **after** the Trello write succeeded and the local DB was updated. If the process crashes before the Trello write, the message is safely re-processed on retry.
- Known limitation: a crash *between* the Trello write and `mark_processed` leads to a retry and a possible duplicate card. The near-duplicate guard (3.3) surfaces it to the user; it is not blocked.
- For queries, `mark_processed` is called after the reply is sent, so a crash keeps the message retryable.
- A duplicate confirmation (same text twice) is ignored via the processed IDs.

### 3.2 Concurrency: FIFO

- **FIFO applies to ingestion only.** Incoming messages per user are interpreted strictly in arrival order (single FIFO queue per user in the `queue` table).
- **Each message is an independent process.** One message never blocks the next. Every message is interpreted on its own, with the per-user conversation history (last 12 turns) as context. The history stores the ID of the last card created or edited by the user, so follow-ups like "assign it to Marco" can be resolved.
- **Confirmations are bound to a `pending_id`.** A reply confirming a reference applies that specific action. A bare "OK"/"YES" with exactly one active pending action confirms it; with several active pendings the bot lists them and asks which one is meant (never guesses).
- Back-to-back messages go through the same FIFO: a message intended as a confirmation is matched against a `pending_id` before being treated as a new instruction.

### 3.3 Near-duplicate guard (reworded repeats)

Message-ID idempotency cannot catch a card the user forgot about and prompts again with different wording. The guard makes the user aware in the outcome message.

- **Scope.** Only `add_card` actions. On every `add_card`, the pipeline compares the new title against (a) the user's active pending `add_card` actions and (b) every open card on the board.
- **Method.** Deterministic, no LLM. Open cards are read from Trello with a GET; titles are normalized (casefold, punctuation stripped, whitespace collapsed) and compared with a sequence-similarity ratio. A ratio >= 0.75 counts as a near-duplicate.
- **Behavior.** Hits are appended to the confirmation message as a heads-up, e.g. *"Heads-up: similar card(s) found: 'Prepare dairy-free cake' (already on the board). Check whether this is a duplicate."* The guard never blocks and never writes.
- **Limits.** Title similarity only: heavily reworded paraphrases with different vocabulary may fall under the threshold. Semantic dedupe (embeddings or LLM-side matching) is a possible later extension.

## 4. Trello

### 4.1 Integration: can everything go through the API?

- Full index of Trello endpoints: `./trello_index.txt` (generated from Trello's OpenAPI spec). Development reference only: it is the map used to decide which actions to support, and is not loaded at runtime.
- Curated catalogue of the actions the LLM can choose from: `./actions_list.txt` (entries: name, human-language description, trigger phrases, input schema; see 2.2 and 2.3). Loaded at runtime: embedded in the LLM system prompt (the LLM may choose only these actions) and used by the deterministic parser to match the action (≥ 75% similarity, else one clarifying question — see 2.3). It contains only the supported actions — the seven listed in 2.3 — and is kept short to save tokens.
- Webhook action types and rate limits (not covered by the OpenAPI spec): kept in a separate development note, e.g. `./trello_notes.md`.

### 4.2 Authentication

Create an API key (Power-Up admin) and generate a token with read/write scope. Use a dedicated "Task Bot" Trello account so bot actions can be told apart from human ones, and add that account only to the boards the bot manages. Every request carries `key` + `token` as query parameters (`TrelloClient._req`); empty values are dropped.

### 4.3 Incoming events (Trello → system): webhooks

Status: **not yet implemented**. Purpose: tell the system about changes it did not make itself.

- Register with `POST /1/webhooks` giving `idModel` (one webhook per board is enough) and a public HTTPS `callbackURL`. Trello sends every event of the observed board; filtering by `action.type` happens in our code.
- The endpoint must answer Trello's initial HEAD request and should verify the HMAC-SHA1 signature header on every call.
- Webhooks fire for the bot's own edits too. Events whose actor (`idMemberCreator`) is the bot's member ID generate **no notifications**, to avoid loops. They may still be applied idempotently to the local DB if a mirror is introduced.
- Human edits made directly in Trello arrive through the same webhook and become notifications to the people involved.
- Webhooks can be disabled after repeated delivery failures: add a health check that re-registers them, plus a periodic heartbeat verifying the webhook is still active.
- Missed events: when webhooks go live, add a full resync on startup and periodically (e.g. daily) to recover events lost during downtime or webhook deactivation.
- Log the real `action.type` values received for a few days before implementing handlers; implement only the ones that are needed.

### 4.4 Limits to design around

- **Reminders.** There is no webhook event for an approaching deadline, and Trello's own due reminders go to Trello's channels (app, email), not to our endpoint. → The Scheduler runs every 10 minutes, queries Trello with a GET (`fields=name,due,dueComplete,idMembers`), selects cards due within the configured reminder windows (default proposal: 24 h and 1 h before due) that are not `dueComplete`, and notifies the members through SimpleX. Each (card, window) pair is notified once, tracked in the `reminders_sent` table.
- **Outgoing messages.** Trello has no chat API. → All user notifications go through SimpleX, built by the Notification builder.
- **Rate limits.** Roughly 300 requests per 10 s per API key and 100 per 10 s per token. → Ample for a team, but prefer webhooks over polling where possible.
- **Board ID forms.** Most endpoints accept the short slug, but `/1/search` (`idBoards`) and `POST /1/lists` need the full 24-char ID → resolve lazily and cache.

## 5. Tech list

| Layer | Options (as implemented) |
| --- | --- |
| Chat interface | SimpleX Chat CLI in WebSocket API mode + Python client |
| Audio | (not yet implemented) ffmpeg + Whisper (optional; live path is text) |
| LLM | Mistral API chat completions with function calling, stdlib only; bounded tool loop (5), per-user history (12 turns) |
| Orchestration | Plain Python, no framework |
| Webhook endpoint | (not yet implemented) FastAPI/Node behind public HTTPS |
| Queue / state | SQLite |
| Scheduler | Asyncio loop every 10 minutes (reminders + pending-action expiry) |
| Storage | Local SQLite; kDrive archive optional |
| Calendar | Reuses the scheduler's Trello GET (due dates of open cards); no separate request |
| Deployment | Single process + simplex-chat CLI; secrets in `CODE/.env`; structured logging to stderr |
| Card backend | Trello REST API |

## 6. Requirements (decisions)

| Requirement | Decision |
| --- | --- |
| User mapping table | SimpleX contact ↔ Trello member (`users` table), filled by the onboarding gate. If a named assignee cannot be resolved, the bot asks a clarifying question. |
| Date handling | Relative dates ("next Tuesday") are resolved in **Europe/Berlin**; converted to UTC for Trello. |
| Languages | Input in **any language**; the bot answers in the user's language. **Card content is always English** (title, description). |
| Idempotency | The same message is never processed twice (see 3.1). A reworded repeat of a forgotten card is flagged by the near-duplicate guard (3.3), never silently duplicated. |
| Concurrency | FIFO for ingestion only; every message is an independent process; confirmations bound to `pending_id`; an ambiguous "OK" with multiple active pendings triggers a clarification. |
| Error handling and retries | Trello write failure → the user gets a clear error and can resend. LLM/API failure → error reply, nothing written. Gateway logs pipeline errors and keeps consuming events. |
| Permissions | The onboarding gate admits only known contacts mapped to Trello members. The bot account is a member only of the boards it manages. |
| Ambiguity policy | Several matching cards, or an unknown or ambiguous assignee/list/label → **no action** and ONE clarifying question listing the candidates. Decided by deterministic checks, not by an LLM confidence score. |
| Privacy / GDPR | **Open decisions:** (1) how long transcripts and conversation history are retained, (2) where they are stored, (3) who can read them. To be decided before production use. |

## 7. Run and clean shutdown

### 7.1 Start

1. Start the SimpleX Chat CLI with the WebSocket API enabled (one process, keeps running):

   ```bash
   simplex-chat -d ~/.simplex/coo-bot.db -p 5225 --user-display-name "COO Assistant"
   ```

   `-p 5225` is the chat server port the bot connects to; `-d` is the CLI database prefix.

2. With `CODE/.env` configured (Trello, Mistral, `SIMPLEX_WS_URL`), start the bot:

   ```bash
   cd CODE
   python3 CODE/scripts/run_bot.py            # logs to stderr; use 2>&1 | tee bot.log to persist
   ```

The bot connects to `SIMPLEX_WS_URL` (default `ws://127.0.0.1:5225`), restores persisted state from `DATABASE_PATH` (default `coo.db`: onboarded contacts, processed message IDs, pending actions, reminders sent, board structure cache), runs the `task_json → card_json` column migration in place on old DBs, and runs the scheduler loop for pending reminders and 24 h expiry. **Run exactly one bot instance**: the CLI delivers each chat event to one client only, so a second instance (or a debug listener) steals events. On restart, kill the old PID first.

### 7.2 Clean shutdown

1. **Stop the bot first**: send SIGINT (Ctrl+C in its terminal, or `kill <pid>`). `scripts/run_bot.py` catches `KeyboardInterrupt`, prints "COO assistant stopped." and exits with code 0. All state is committed to SQLite at each step, so stopping at any point loses nothing: on the next start the bot resumes from the persisted DB.
2. **Then stop `simplex-chat`** (Ctrl+C or `pkill -f simplex-chat`).

Order matters: stop the bot before the SimpleX CLI, so no incoming message is left half-processed between gateway and pipeline.
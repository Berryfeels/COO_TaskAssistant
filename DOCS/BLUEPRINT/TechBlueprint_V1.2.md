# COO Task Management Tool — Technical Blueprint V1.2 (audio messages + STT model voxtral-mini-transcribe-3)

Full blueprint, derived from `TechBlueprint_V1.1.md`.

**Changes in V1.2 — STT model for long audio (the audio implementation itself is V1.1, unchanged):**
- **STT model `voxtral-mini-transcribe-3`** via `MISTRAL_STT_MODEL` in `CODE/.env`: the batch transcribe model documented for **long recordings up to 3 hours in a single request**. `voxtral-mini-transcribe-2` is retired (verified live against the API: HTTP 400 `invalid_model`); `voxtral-mini-latest` and `voxtral-small-transcribe-3` also answer 200 on short files.
- **STT HTTP timeout raised 120 s → 600 s** (`app/audio/mistral_stt.py`): long recordings can take minutes server-side before the first response byte.
- File cap of the transcription API stays **500 MB**; beyond 3 h, chunk the audio inside the `transcriber` hook (see 2.5.1).

**Changes in V1.1 — audio (voice) messages:**
- **Audio is implemented and live** (V1.0: "not yet implemented"). The design principle stays: the Orchestrator LLM takes only text; audio is transcribed *before* the pipeline, at the gateway (new section 2.5).
- **STT engine: Mistral Voxtral via REST** (`POST https://api.mistral.ai/v1/audio/transcriptions`, multipart, stdlib only) — not ffmpeg+Whisper. Model from `MISTRAL_STT_MODEL`, set to **`voxtral-mini-transcribe-3`** in `CODE/.env` (V1.2). The transcriber is a swappable hook (`gateway.transcriber`); the chatBHT STT service can replace it later without touching the gateway.
- **New component:** `app/audio/mistral_stt.py` (`MistralSTT`), wired in `scripts/run_bot.py`; gateway media seam in `app/gateway/simplex.py` (`_extract_media` / `_handle_media` / `receive_file`).
- **File download from the SimpleX CLI** over the same WebSocket: `/freceive <fileId>`, path resolved from the `rcvFileComplete`/updated-`newChatItems` event or a filesystem poll (cwd, `~/Downloads`). Media handling runs as a background task — awaiting it in the read loop self-deadlocks (regression-tested).
- **Operational requirement (multi-user):** SimpleX apps ≥ v6.2 use two preset operators — SimpleX (`*.simplex.im`) and Flux (`*.simplexonflux.com`) — with 6 preset XFTP servers each (12 total). The CLI only receives files whose servers are in its known list (`protocol_servers` in `<profile>_chat.db`); otherwise `/freceive` fails with `fileNotApproved`/`unknownServers`. The bot's CLI profile must know **all 12 preset XFTP servers** (see 2.5.5 and 7.1).
- **Known gap (to fix):** `receive_file` does not inspect the `/freceive` command response for `chatCmdError`, so a refused file surfaces only after the 120 s timeout as a bare `TimeoutError` instead of the real `fileNotApproved` reason (see 2.5.4).
- Privacy note: audio bytes are sent to `api.mistral.ai` for transcription (see 6).

## 1. Description

A digital COO executive assistant. It engages with the team's Trello board automatically and creates tasks on behalf of the COO after meetings. It supports the COO and each team member in completing tasks through chat answers, reminders and notifications.

**Features:** answer chat; accept **text and voice messages**; create a card; create a list; update a card; monitor deadlines; notification system.

### Design principles

- **Orchestrator (LLM) takes only text.** Audio is transcribed before it reaches the LLM (implemented in V1.1, see 2.5).
- **Language.** The user can write or speak in any language and the bot answers in the user's language. Text written to Trello (titles, descriptions, comments) is always English.
- **Trello is the single source of truth.** Every Trello write happens first; the local DB is updated only after the write succeeded.
- **Write confirmation comes from the Trello API response**, not from a webhook. Webhooks (once live) are used for human edits made directly in Trello and, optionally, for cache synchronization.
- **Naming mirrors Trello.** Software objects and functions use Trello's vocabulary: card, list, member, label, due, checklist, comment.
- **Time.** All dates and times are interpreted in `Europe/Berlin` (CET/CEST, including daylight saving). Trello stores dates in UTC: convert at the boundaries (LLM/user side ↔ Trello side).

## 2. Architecture

### 2.1 Flow

1. **Message in.** A SimpleX chat event arrives. Text items go straight to the pipeline; **audio/voice items are downloaded from the CLI and transcribed to text at the gateway** (2.5), then the transcript enters the pipeline as if it were a text message (same `handle_incoming`, same message id).
2. **Pipeline.** FIFO queue → idempotency check → legacy confirmation match → Orchestrator LLM (intent + extraction).
3. **Orchestrator output.** The LLM answers in **plain words, never JSON**: for a board action, one labelled line per field (`action:`, `title:`, `card:`, `assign:`, `due:`, `list:`, `note:`, `question:`) using names *as the user said them* (list name, member name, card name); for pure conversation, free prose with no `action:` line. Function calling is used only for the read tools (`board_cards`, `search_cards`, `get_card`, `list_members`, `list_lists`). The LLM never handles Trello IDs.
4. **Validation and resolution (deterministic code).** The parser fills the `Card` JSON from the LLM's words, matches the `action:` line against the catalogue `./actions_list.txt` (≥ 75% similarity selects the action; below, or two equally close, ONE clarifying question to the user — see 2.3), validates against the schema and the board state, resolves names to IDs with fuzzy matching using the cached board structure (2.4), and returns clear errors to the LLM when something does not match.
5. **Write step.** Deterministic code builds the Trello API call and executes it. The API response confirms the outcome.
6. **After success.** The local DB is updated, `mark_processed` is called, and the bot sends a confirmation message to the user (e.g. "Done: 'Call supplier' card has been created.").
7. **Monitoring.** A scheduler regularly checks upcoming due dates and notifies the people involved through SimpleX.

### 2.2 Components

- **SimpleX Gateway (in/out):** the real message entry point, including the **audio seam**: media extraction, file download from the CLI and the transcriber hook (2.5).
- **STT (speech-to-text):** `app/audio/mistral_stt.py` — Mistral Voxtral via REST, stdlib only. Injected into the gateway as the `transcriber` callable by `scripts/run_bot.py`; swappable for the chatBHT STT service later.
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

### 2.5 Audio messages (voice) — implementation (V1.1)

The LLM stays text-only: **transcription happens in the gateway, before the pipeline** (blueprint principle, section 1). Chain:

```
newChatItems event (voice item)
  → SimpleXGateway._extract_media()        app/gateway/simplex.py   non-text directRcv items → (user_id, itemId, kind, {msgContent, file})
  → on_chat_event() spawns _handle_media   background task — awaiting inline would deadlock the read loop (2.5.2)
  → transcriber hook                       scripts/run_bot.py: transcribe(payload)
       → gateway.receive_file(fileId, fileName, fileSize)   "/freceive <fileId>" over the same WS (2.5.3)
       → MistralSTT.transcribe_file(path)    app/audio/mistral_stt.py   multipart POST to /v1/audio/transcriptions (2.5.1)
  → transcript text → pipeline.handle_incoming(user_id, itemId, transcript)   same path as a text message
```

#### 2.5.1 STT client (`app/audio/mistral_stt.py`)

- `POST https://api.mistral.ai/v1/audio/transcriptions`, `multipart/form-data`: `model` (`MISTRAL_STT_MODEL`, set to `voxtral-mini-transcribe-3` in `CODE/.env` since V1.2; `voxtral-mini-latest` was the V1.1 default), `response_format=json`, optional `language`, `file` (the audio bytes, content-type guessed from the filename). `Authorization: Bearer MISTRAL_API_KEY`. Stdlib only (`urllib.request`), HTTP timeout **600 s** (V1.2; was 120 s in V1.1 — long recordings can take minutes server-side before the first response byte).
- **Long recordings (V1.2):** the Transcribe batch models accept **up to 3 h of audio in one request** (Mistral docs); the hard file cap is **500 MB**, and the client still reads the whole file + multipart body into RAM (~2× file size — fine up to hundreds of MB). So 1–1.5 h recordings (≈15–90 MB as m4a/opus) work in a single call. Beyond 3 h or for very large files, chunk the audio (e.g. `ffmpeg -f segment -segment_time 600`) inside the `transcriber` hook and merge the transcripts. Caveat: the XFTP download in `receive_file` still has a 120 s wait — very large files on slow links may need a larger timeout there too. Note: SimpleX apps cap *voice notes* at ~5 min, so long recordings arrive as **files**, which the gateway handles identically (2.5.2).
- Response `{"text": "..."}` → the stripped transcript. Missing API key raises before any request.
- Verified live: a 4 s `.m4a` voice note returns HTTP 200 in ~0.3–0.4 s with the correct text.

#### 2.5.2 Gateway media path (`app/gateway/simplex.py`)

- `_extract_media(ev)` surfaces every received **non-text** direct item (voice, image, file, …) as `(user_id, message_id, kind, payload)`; the file reference is read from `chatItem.file` (sibling of `content`) or, in older shapes, nested in `msgContent.file`. Payload: `{"msgContent": {...}, "file": {"fileId", "fileName", "fileSize", ...}}`.
- `on_chat_event()` **spawns** `_handle_media` as a background task (`_spawn`). Awaiting it inline self-deadlocks: `receive_file` waits for a frame (`rcvFileComplete`) on the very socket the read loop (`async for raw in ws`) is blocked on. Regression test: `test_media_handling_does_not_block_the_read_loop` in `tests/UNIT/test_gateway.py`.
- `_handle_media()` behavior, never silent:
  - no transcriber configured → reply "I can only read text messages for now…";
  - transcriber raises → reply "Sorry: I could not transcribe that … (<reason>)";
  - empty transcript → reply "I could not understand that audio…";
  - otherwise the onboarding gate (`admit`) runs on the transcript, then `pipeline.handle_incoming(user_id, message_id, transcript)`. The media item's `itemId` is reused as `message_id`, so a redelivered event is deduplicated by the usual `already_processed` check (3.1).

#### 2.5.3 Downloading the file from the CLI (`receive_file`)

`receive_file(file_id, file_name, file_size, timeout=120)`:

1. registers a future in `_file_waiters[fileId]`, sends `/freceive <fileId>` (`_send_cmd`, 30 s timeout) over the WebSocket;
2. races two resolvers (`asyncio.wait(..., FIRST_COMPLETED)`):
   - **event resolver:** every incoming event is scanned (`_scan_file_event`/`_scan_files`) for a dict with `fileId` + `filePath` (also nested under `fileSource.filePath`) + a status containing `complete` (e.g. `rcvFileComplete` with `fileStatus: rcvComplete`) → resolves the local path;
   - **filesystem poll** (only if `fileName` is present): `_poll_file` scans the CLI download dirs — **cwd and `~/Downloads`** — every 0.5 s until the file appears with the expected size stable across two polls. Immune to event-shape differences between CLI versions.
3. relative reported paths are resolved against the same dirs (`_resolve_path`).

The downloaded file lands in the CLI's files folder (in our deployment `~/Downloads`; visible as `file_path` in the CLI's `files` table).

#### 2.5.4 Known gap (fix planned)

`_send_cmd` returns the command response as a plain dict and `receive_file` never checks it: when `/freceive` is refused (`{"type":"chatCmdError","chatError":{"type":"error","errorType":{"type":"fileNotApproved","fileId":N,"unknownServers":[...]}}}`) the waiter is registered, so the "command failed" log branch is skipped, and the gateway waits the full 120 s before raising a bare `TimeoutError`. The user then gets a misleading "could not transcribe (TimeoutError)". **Fix:** inspect the `/freceive` response for `chatCmdError`/`error` and fail fast with the real reason (e.g. `fileNotApproved`, with the `unknownServers` list).

#### 2.5.5 Multi-user requirement: known XFTP servers in the CLI profile (operational)

SimpleX apps **≥ v6.2** offer two preset operators, and use servers of both: **SimpleX Chat Ltd (`*.simplex.im`)** and **Flux / InFlux Technologies (`*.simplexonflux.com`)**. Preset XFTP (file) servers: `xftp1–6.simplex.im` and `xftp1–6.simplexonflux.com` — **12 public preset XFTP servers**; the sending app picks ~3 random ones per operator per file, so over time any of the 12 can appear. Apps older than v6.2 use only `*.simplex.im`. On top of these, users may configure custom/community/self-hosted servers (not enumerable).

The CLI only receives files whose servers are in its **known server list** — table `protocol_servers` in the chat store `<profile>_chat.db` (e.g. `~/.simplex/coo-bot.db_chat.db`), plus `server_operators`. A file on an unknown server makes `/freceive` fail with `fileNotApproved` + `unknownServers` (observed live: every voice note uploaded to `*.simplexonflux.com` was stuck as `rcv_invitation` while `*.simplex.im` files downloaded fine). **For compatibility with any phone on default settings, the bot's CLI profile must contain all 12 preset XFTP servers** (host `xftpN.<domain>,<onion>` + `key_hash`; the official list with key hashes is in `simplex-chat` `src/Simplex/Chat/Operators/Presets.hs`, `fluxXFTPServers` / `defaultXFTPServers`). Add them via the CLI's own server management (`/xftp` in its terminal or over the WS API) — or, with the CLI stopped, by inserting rows into `protocol_servers` (and the Flux row into `server_operators`) — then **restart the CLI**. Verified live: after adding the Flux servers, a previously refused file (`/freceive 11`) returned `rcvFileAccepted → rcvFileComplete` and landed in `~/Downloads` with status `rcv_complete`. Custom servers of individual users still require per-file approval (`fileNotApproved` flow). Note: the `.onion` halves need a SOCKS proxy (`--socks-proxy` + Tor); without one only the clearnet hosts are used — sufficient, since clearnet 443 to both domains is reachable.

#### 2.5.6 Tests

- `tests/UNIT/test_mistral_stt.py`: multipart request shape (model, `response_format`, file part, content-type, closing boundary), Bearer auth header, `language` field, stripped `text` response, missing API key raises before any request, `transcribe_file` reads from disk.
- `tests/UNIT/test_gateway.py` (`TestMediaHandling`, `TestReceiveFile`): media extracted separately from text; file ref nested in `msgContent` still found; no transcriber → text-only reply; transcriber transcript → fed to the pipeline; `admit` gate respected; transcriber failure / empty transcript → error reply, never silence; read-loop deadlock regression; `/freceive` sent over the socket; resolution via `rcvFileComplete`, via updated `newChatItems`, and via `fileSource.filePath`.

## 3. Message management

### 3.1 Idempotency and `mark_processed` semantics

`mark_processed(msg.id)` means: *this incoming message has been handled and must never be processed again*. It is **not** a confirmation and does **not** imply the action was executed.

- For write actions, `mark_processed` is called only **after** the Trello write succeeded and the local DB was updated. If the process crashes before the Trello write, the message is safely re-processed on retry.
- Known limitation: a crash *between* the Trello write and `mark_processed` leads to a retry and a possible duplicate card. The near-duplicate guard (3.3) surfaces it to the user; it is not blocked.
- For queries, `mark_processed` is called after the reply is sent, so a crash keeps the message retryable.
- A duplicate confirmation (same text twice) is ignored via the processed IDs. The same holds for a redelivered voice event: its transcript carries the media item's `itemId`, so it is deduplicated here (2.5.2).

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
| Audio | **Implemented (V1.1), model upgraded in V1.2:** Mistral Voxtral STT via REST (`POST /v1/audio/transcriptions`, multipart, stdlib only; `MISTRAL_STT_MODEL=voxtral-mini-transcribe-3` in `CODE/.env` — long recordings up to 3 h per request, file cap 500 MB; HTTP timeout 600 s). Files are fetched from the CLI with `/freceive` over the same WebSocket. chatBHT STT service is the planned in-house replacement — swap only the `transcriber` hook (2.5). |
| LLM | Mistral API chat completions with function calling, stdlib only; bounded tool loop (5), per-user history (12 turns) |
| Orchestration | Plain Python, no framework |
| Webhook endpoint | (not yet implemented) FastAPI/Node behind public HTTPS |
| Queue / state | SQLite |
| Scheduler | Asyncio loop (hourly in the current deployment) for reminders + pending-action expiry |
| Storage | Local SQLite; kDrive archive optional |
| Calendar | Reuses the scheduler's Trello GET (due dates of open cards); no separate request |
| Deployment | Single process + simplex-chat CLI; secrets in `CODE/.env`; structured logging to stderr |
| Card backend | Trello REST API |

## 6. Requirements (decisions)

| Requirement | Decision |
| --- | --- |
| User mapping table | SimpleX contact ↔ Trello member (`users` table), filled by the onboarding gate. If a named assignee cannot be resolved, the bot asks a clarifying question. |
| Date handling | Relative dates ("next Tuesday") are resolved in **Europe/Berlin**; converted to UTC for Trello. |
| Languages | Input in **any language** (typed or spoken — the transcript is just text by then); the bot answers in the user's language. **Card content is always English** (title, description). |
| Audio input | Voice messages are transcribed at the gateway (2.5) and then follow the exact text path: same idempotency (`itemId`), same FIFO, same LLM. Failure modes always produce a user-facing reply, never silence. |
| Idempotency | The same message is never processed twice (see 3.1). A reworded repeat of a forgotten card is flagged by the near-duplicate guard (3.3), never silently duplicated. |
| Concurrency | FIFO for ingestion only; every message is an independent process; confirmations bound to `pending_id`; an ambiguous "OK" with multiple active pendings triggers a clarification. Media handling runs as background tasks and never blocks the gateway read loop (2.5.2). |
| Error handling and retries | Trello write failure → the user gets a clear error and can resend. LLM/API failure → error reply, nothing written. STT failure or empty transcript → clear reply asking for text (2.5.2). Gateway logs pipeline errors and keeps consuming events. Known gap: `/freceive` refusals are reported as a generic timeout until 2.5.4 is fixed. |
| Permissions | The onboarding gate admits only known contacts mapped to Trello members. The bot account is a member only of the boards it manages. The CLI profile must know all 12 preset public XFTP servers (both operators) so any phone on default settings can send files (2.5.5). |
| Ambiguity policy | Several matching cards, or an unknown or ambiguous assignee/list/label → **no action** and ONE clarifying question listing the candidates. Decided by deterministic checks, not by an LLM confidence score. |
| Privacy / GDPR | **Open decisions:** (1) how long transcripts and conversation history are retained, (2) where they are stored, (3) who can read them. To be decided before production use. **V1.1 addition:** voice-message audio bytes are sent to `api.mistral.ai` (Voxtral) for transcription — add Mistral's API data-retention/processing terms to this decision; the chatBHT in-house STT would remove the external hop. |

## 7. Run and clean shutdown

### 7.1 Start

1. Start the SimpleX Chat CLI with the WebSocket API enabled (one process, keeps running):

   ```bash
   simplex-chat -d ~/.simplex/coo-bot.db -p 5225 --user-display-name "COO Assistant"
   ```

   `-p 5225` is the chat server port the bot connects to; `-d` is the CLI database prefix. **The CLI profile must know all 12 preset public XFTP servers** (`xftp1–6.simplex.im` + `xftp1–6.simplexonflux.com`, with key hashes — see 2.5.5); check `protocol_servers` in `~/.simplex/coo-bot.db_chat.db` and add the missing ones via the CLI's `/xftp` server management (or by SQL with the CLI stopped). **Restart the CLI after changing its server list.** Received files download into the CLI's files folder (here `~/Downloads`).

2. With `CODE/.env` configured (Trello, Mistral incl. `MISTRAL_API_KEY`/`MISTRAL_STT_MODEL`, `SIMPLEX_WS_URL`), start the bot:

   ```bash
   cd CODE
   python3 scripts/run_bot.py            # logs to stderr; use 2>&1 | tee bot.log to persist
   ```

The bot connects to `SIMPLEX_WS_URL` (default `ws://127.0.0.1:5225`), restores persisted state from `DATABASE_PATH` (default `coo.db`: onboarded contacts, processed message IDs, pending actions, reminders sent, board structure cache), runs the `task_json → card_json` column migration in place on old DBs, wires the STT transcriber (`gateway.transcriber = transcribe`, section 2.5), and runs the scheduler loop for pending reminders and 24 h expiry. **Run exactly one bot instance**: the CLI delivers each chat event to one client only, so a second instance (or a debug listener) steals events. On restart, kill the old PID first.

### 7.2 Clean shutdown

1. **Stop the bot first**: send SIGINT (Ctrl+C in its terminal, or `kill <pid>`). `scripts/run_bot.py` catches `KeyboardInterrupt`, prints "COO assistant stopped." and exits with code 0. All state is committed to SQLite at each step, so stopping at any point loses nothing: on the next start the bot resumes from the persisted DB. An in-flight voice message may remain un-transcribed; its event is re-read from the CLI on restart only if not yet acked — otherwise resend the voice note.
2. **Then stop `simplex-chat`** (Ctrl+C or `pkill -f simplex-chat`).

Order matters: stop the bot before the SimpleX CLI, so no incoming message is left half-processed between gateway and pipeline.

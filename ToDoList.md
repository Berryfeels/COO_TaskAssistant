# ToDoList — Build Order (TechBlueprint V5, section 9.2)

Working document: update the checkboxes as work progresses. The detailed
rationale lives in the blueprint; this file tracks status only.

Legend: [x] done, [ ] todo.

## Step 1 — Trello service and Task schema, tested against a sandbox board

- [x] Trello REST API wrapper (create/update/archive/search/members, `TrelloClient`)
- [x] Task schema mirroring a card (spec section 4), with reverse mapping
- [x] FakeTrello for offline tests
- [x] Sandbox board test, both mirror directions (`scripts/trello_sandbox_test.py`)

## Step 2 — Orchestrator with text input only, including card search and confirmation loop

- [x] Orchestrator behind a Protocol (MockLLM for tests, MistralLLM for real runs)
- [x] Trello read tools for card resolution (board_cards, search_cards, get_card, list_members)
- [x] Confirmation loop before any write (spec 5.3)
- [x] Real-env conversation test: Mistral + pipeline + sandbox board (`scripts/mistral_conversation_test.py`)
- [x] Multilingual input, English card content, field-distribution policy (`scripts/italian_scenario_test.py`)

## Step 3 — SimpleX gateway with FIFO queue, so the bot works end to end with text

- [x] FIFO queue per user (`app/queueing.py`)
- [x] Pipeline: handle_incoming / confirmation flow with a Gateway stub (`app/pipeline.py`)
- [ ] SimpleX gateway: WebSocket client to the SimpleX Chat CLI/bot API (receive messages, send replies; replaces the Gateway stub)
- [ ] User mapping: SimpleX contact <-> Trello member (users table)
- [ ] End-to-end test: text message in SimpleX -> card on the board

## Step 4 — Pending lifecycle: confirmation, 24h expiry, 3 daytime reminders, cancellation notice

- [x] Confirmations bound to pending_id, 24h TTL
- [x] 3 reminders every 7 hours, never 02:00-05:00 Berlin
- [x] Cancellation notice on expiry
- [x] Near-duplicate guard for reworded repeats (spec 5.4, added during step 2/4 work)

## Step 5 — Whisper transcription for audio

- [ ] faster-whisper service (audio -> text), language auto-detect
- [ ] Wire into the pipeline: transcript instead of raw text when the message has audio
- [ ] Transcript archive handoff (feeds step 7)

## Step 6 — Webhook receiver, loop prevention and notifications for human edits

- [ ] FastAPI endpoint: HEAD check + HMAC-SHA1 signature verification
- [ ] Event parser: card created/updated/moved/completed/commented/member added
- [ ] Loop prevention: ignore events whose actor is the bot's member ID
- [ ] Notifications to the people involved via SimpleX
- [ ] Webhook health check + re-registration heartbeat

## Step 7 — kDrive archive, GDPR retention rules, deployment and monitoring

- [ ] Transcript archive to kDrive via WebDAV (one .txt per message, named by date + message ID)
- [ ] GDPR retention rules for audio/transcripts
- [ ] Deployment (Docker Compose on VPS, secrets, structured logging)
- [ ] Monitoring

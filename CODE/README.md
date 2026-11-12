# COO Assistant

Voice/text assistant that manages team cards in Trello via SimpleX Chat.
Spec: see "COO Task Management Tool - Revised Specification v3".

## Design principles

- LLM only where language understanding is needed; everything else is deterministic code.
- Trello is the single source of truth. No write without user confirmation.
- Every event processed exactly once (message IDs stored).
- FIFO per user for ingestion; each message is an independent pending process.
- Input in any language; card content always in English.
- A reworded repeat of a forgotten card is flagged in the confirmation with a
  deterministic near-duplicate check (title similarity); it is never silently
  duplicated and never blocked - the user decides.

## Status of this build (build-order steps 1-4, plus the SimpleX gateway of step 3)

1. Trello service (API wrapper, mockable) and Card schema.
2. Orchestrator interface with card search and confirmation loop (MistralLLM behind a Protocol).
3. FIFO queue and pipeline (text input).
4. Pending lifecycle: confirmations bound to pending_id, 24h expiry, 3 daytime reminders (07:00-02:00 Berlin, never 02:00-05:00), cancellation notice.
5. Card field distribution via MistralLLM: any-language input, English card
   content, auto-generated English title, sentence subject as responsible
   member, date+time with Berlin offset in `due`, details in `description`;
   near-duplicate guard over active pendings and open cards.
6. SimpleX gateway (build step 3): WebSocket client to the SimpleX Chat CLI
   API, contact onboarding (SimpleX contact <-> Trello member, persisted in
   SQLite), and `scripts/run_bot.py` wiring the real components end to end
   with an hourly reminder/expiry scheduler.

Not yet: Whisper audio, Trello webhooks, kDrive archive.

## Layout

    app/
      config.py             settings from environment
      schema.py             Card model (spec section 4)
      dates.py              relative date resolution, Europe/Berlin
      similarity.py         near-duplicate title guard (spec 5.4)
      storage/db.py         SQLite: users, processed, pending
      pipeline.py           handle_incoming / confirmation flow (spec 9.1)
      orchestrator/
        agent.py            LLMProvider protocol (MistralLLM is the implementation)
        mistral_llm.py      MistralLLM: real LLM via Mistral API (stdlib only);
                            any input language, English card content
      gateway/
        simplex.py          SimpleX Chat CLI WebSocket client: receive chat
                            events, send replies, reconnect on drop
        onboarding.py       SimpleX contact <-> Trello member mapping
      confirm/
        pending.py          save / pop / expire / reminders / cancel
      trello/
        client.py           TrelloClient (REST API wrapper)
      notify/
        builder.py          render_confirmation / result / reminder / cancelled
    scripts/
      run_bot.py            end-to-end entry point (no mocks): gateway +
                            pipeline + hourly reminder/expiry scheduler

## Test against a real Trello sandbox board

    export TRELLO_KEY=... TRELLO_TOKEN=... TRELLO_DEFAULT_LIST_ID=...
    export TRELLO_BOARD_SANDBOX_ID=...   # recommended: scopes search to one board
    python3 scripts/trello_sandbox_test.py

(The scripts load `CODE/.env` automatically; exporting is only needed to
override what is already in `.env`.)

Creates one test card, audits the mirror in both directions
(Card model <-> Trello card dict via `from_api`/`to_api`), moves the date, completes, archives,
searches, then deletes the card. Use a sandbox board, never production.

## Test a rich multilingual message end-to-end (real Mistral + Trello)

    export MISTRAL_API_KEY=... TRELLO_KEY=... TRELLO_TOKEN=...
    export TRELLO_BOARD_SANDBOX_ID=... TRELLO_DEFAULT_LIST_ID=...
    python3 scripts/italian_scenario_test.py

Sends the Italian message "Stefano deve fare una torta per martedi 17
Novembre ..." through the real MistralLLM, pipeline and sandbox board.
Verifies the field-distribution policy (English auto-generated title,
Stefano as responsible member, due 2026-11-17 08:00 Berlin, every detail in
the description in English) plus confirmation and mirror audit. The card is
NOT deleted: it stays on the board for review in the Trello dashboard
(remove it manually).

## Run the bot end to end (text)

One-time: a local simplex-chat CLI with the WebSocket API enabled, e.g.

    simplex-chat -d ~/.simplex/coo-bot.db -p 5225 --user-display-name "COO Assistant"

Then, with `.env` configured (see below):

    python3 CODE/scripts/run_bot.py

Wires the real components with no mocks (SimpleXGateway <-> Pipeline <->
MistralLLM <-> TrelloClient) and runs an hourly loop for pending reminders
and 24h expiry. Unknown contacts are onboarded by display name before their
first message is processed; onboarding, processed message IDs and pending
actions survive restarts in SQLite.

Do not attach a second WebSocket client to the same simplex-chat port while
the bot is running: the CLI delivers each chat event to one client only, so
a debug listener would steal events from the bot.

## Run unit tests (offline, no API keys)

    python3 -m pytest tests/

The remaining tests are offline with stubbed urlopen only: date parsing and
Card validation, notification text, SimpleX event parsing, and the exact HTTP
request shapes of the real TrelloClient. Everything else is tested against
the real services: Mistral LLM + real Trello board via the scripts in
`scripts/` (see the usage notes in each script).

## Configure

`.env` (git-ignored, never committed) holds the settings for real runs:

    # Trello
    TRELLO_KEY, TRELLO_TOKEN, TRELLO_BOARD_ID
    TRELLO_DEFAULT_LIST_ID                   # where creates land when no column is named
    TRELLO_BOARD_SANDBOX_ID                  # sandbox board used by scripts/*_test.py;
                                             # the bot itself (run_bot.py) runs on
                                             # TRELLO_BOARD_ID
    # Mistral LLM
    MISTRAL_API_KEY, MISTRAL_MODEL
    # SimpleX gateway (used by scripts/run_bot.py)
    SIMPLEX_WS_URL                           # default ws://127.0.0.1:5225
    # Storage, timezone and pending lifecycle (spec 5.1)
    DATABASE_PATH, TIMEZONE,
    PENDING_TTL_HOURS, PENDING_REMINDER_INTERVAL_HOURS,
    PENDING_MAX_REMINDERS, REMINDER_QUIET_START, REMINDER_QUIET_END

# COO Assistant

Voice/text assistant that manages team tasks in Trello via SimpleX Chat.
Spec: see "COO Task Management Tool - Revised Specification v3".

## Design principles

- LLM only where language understanding is needed; everything else is deterministic code.
- Trello is the single source of truth. No write without user confirmation.
- Every event processed exactly once (message IDs stored).
- FIFO per user for ingestion; each message is an independent pending process.
- Input in any language; card content always in English.
- A reworded repeat of a forgotten task is flagged in the confirmation with a
  deterministic near-duplicate check (title similarity); it is never silently
  duplicated and never blocked - the user decides.

## Status of this build (phases 1-4 of the build order)

1. Trello service (API wrapper, mockable) and Task schema.
2. Orchestrator interface with card search and confirmation loop (LLM behind a Protocol; MockLLM for tests, MistralLLM for real runs).
3. FIFO queue and pipeline (text input).
4. Pending lifecycle: confirmations bound to pending_id, 24h expiry, 3 daytime reminders (07:00-02:00 Berlin, never 02:00-05:00), cancellation notice.
5. Task field distribution via MistralLLM: any-language input, English card
   content, auto-generated English title, sentence subject as responsible
   member, date+time with Berlin offset in `due`, details in `description`;
   near-duplicate guard over active pendings and open cards.

Not yet: SimpleX gateway, Whisper audio, Trello webhooks, scheduler daemon, kDrive archive.

## Layout

    app/
      config.py             settings from environment
      schema.py             Task model (spec section 4)
      dates.py              relative date resolution, Europe/Berlin
      similarity.py         near-duplicate title guard (spec 5.4)
      storage/db.py         SQLite: users, processed, pending, queue
      queueing.py           FIFO per user
      pipeline.py           handle_incoming / confirmation flow (spec 9.1)
      orchestrator/
        agent.py            Orchestrator protocol + MockLLM (tests)
        mistral_llm.py      MistralLLM: real LLM via Mistral API (stdlib only);
                            any input language, English card content
      confirm/
        pending.py          save / pop / expire / reminders / cancel
      trello/
        client.py           TrelloClient (REST) + FakeTrello for tests
      notify/
        builder.py          render_confirmation / result / reminder / cancelled

## Test against a real Trello sandbox board

    export TRELLO_KEY=... TRELLO_TOKEN=... TRELLO_SANDBOX_LIST_ID=...
    export TRELLO_SANDBOX_BOARD_ID=...   # recommended: scopes search to one board
    python3 scripts/trello_sandbox_test.py

Creates one test card, audits the mirror in both directions
(Task -> card and card -> Task), moves the date, completes, archives,
searches, then deletes the card. Use a sandbox board, never production.

## Test a rich multilingual message end-to-end (real Mistral + Trello)

    export MISTRAL_API_KEY=... TRELLO_KEY=... TRELLO_TOKEN=...
    export TRELLO_SANDBOX_BOARD_ID=... TRELLO_SANDBOX_LIST_ID=...
    python3 scripts/italian_scenario_test.py

Sends the Italian message "Stefano deve fare una torta per martedi 17
Novembre ..." through the real MistralLLM, pipeline and sandbox board.
Verifies the field-distribution policy (English auto-generated title,
Stefano as responsible member, due 2026-11-17 08:00 Berlin, every detail in
the description in English) plus confirmation and mirror audit. The card is
NOT deleted: it stays on the board for review in the Trello dashboard
(remove it manually).

## Run unit tests (no dependencies needed)

    python -m unittest discover -s tests -v

## Configure

Copy `.env.example` to `.env` and fill in Trello/LLM credentials for real runs.

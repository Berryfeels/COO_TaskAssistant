#!/usr/bin/env python3
"""Run the COO assistant end to end with text (build step 3).

Requirements:
- a local simplex-chat CLI with the WebSocket API enabled (SIMPLEX_WS_URL,
  default ws://127.0.0.1:5225)
- TRELLO_KEY / TRELLO_TOKEN / TRELLO_BOARD_ID and MISTRAL_API_KEY in .env

    python3 scripts/run_bot.py

Loads .env from the CODE directory, wires the real components (no mocks):
SimpleXGateway <-> Pipeline <-> MistralLLM <-> TrelloClient, plus an
hourly loop running the pending reminder and expiry jobs (spec 5.1).
The SQLite DB is persistent (DATABASE_PATH, default coo.db in CODE/):
onboarded contacts, processed message IDs and pending actions survive
restarts.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def load_dotenv() -> None:
    """Minimal .env loader: KEY=VALUE lines, no override of real env."""
    path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


async def scheduler(pipeline) -> None:
    """Hourly reminder + expiry jobs (spec 5.1; quiet hours are honored
    inside PendingService.reminder_job via pipeline.reminder_job)."""
    while True:
        await asyncio.sleep(3600)
        try:
            await pipeline.reminder_job()
            await pipeline.expiry_job()
        except Exception as e:                   # never kill the bot loop
            print(f"scheduler error: {e!r}")


async def main_async() -> None:
    from app.config import settings
    from app.confirm.pending import PendingService
    from app.gateway.onboarding import Onboarding
    from app.gateway.simplex import SimpleXGateway
    from app.orchestrator.mistral_llm import MistralLLM
    from app.pipeline import Pipeline
    from app.storage.db import DB
    from app.trello.client import TrelloClient

    db = DB(settings.database_path)              # PERSISTENT: restarts keep state
    trello = TrelloClient(settings.trello_key, settings.trello_token,
                          board_id=settings.trello_board_id,
                          default_list_id=settings.trello_default_list_id)
    llm = MistralLLM(api_key=settings.mistral_api_key,
                     model=settings.mistral_model)
    pending = PendingService(db, settings)
    gateway = SimpleXGateway(settings.simplex_ws_url)
    pipeline = Pipeline(db, trello, llm, pending, gateway, settings)
    gateway.pipeline = pipeline
    gateway.admit = Onboarding(db, trello, gateway).admit

    print(f"COO assistant: connecting to SimpleX at {settings.simplex_ws_url}")
    await asyncio.gather(gateway.run(), scheduler(pipeline))


def main() -> int:
    load_dotenv()
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\nCOO assistant stopped.")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())

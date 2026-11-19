"""Audio input: speech-to-text for the SimpleX gateway's transcriber hook.

Currently backed by Mistral's audio transcription API (Voxtral models) so
voice messages work NOW; when the chatBHT STT service is embedded, swap the
transcriber in scripts/run_bot.py - the SimpleX gateway itself does not
change (blueprint: the Orchestrator takes only text; audio is transcribed
before it reaches the LLM).
"""

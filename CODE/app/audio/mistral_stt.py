"""Mistral speech-to-text (Voxtral) via the REST API, stdlib only.

Endpoint: POST https://api.mistral.ai/v1/audio/transcriptions
multipart/form-data: model (voxtral-*), file (the audio), optional language,
response_format=json -> {"text": "..."}.

Used as the `transcriber` hook of SimpleXGateway until the chatBHT STT
service is embedded; the gateway then just gets a different callable.
"""
from __future__ import annotations

import json
import mimetypes
import os
import urllib.request
import uuid

from ..config import settings

API_URL = "https://api.mistral.ai/v1/audio/transcriptions"


class MistralSTT:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or settings.mistral_api_key
        self.model = model or settings.mistral_stt_model

    def transcribe_file(self, path: str, language: str | None = None) -> str:
        with open(path, "rb") as fh:
            data = fh.read()
        return self.transcribe_bytes(data, os.path.basename(path), language)

    def transcribe_bytes(self, data: bytes, filename: str = "audio.ogg",
                         language: str | None = None) -> str:
        if not self.api_key:
            raise RuntimeError("MISTRAL_API_KEY is not set - cannot transcribe")
        boundary = "----vibeSTT" + uuid.uuid4().hex
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        body = bytearray()
        for key, value in (("model", self.model),
                           ("response_format", "json")):
            body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                     f'name="{key}"\r\n\r\n{value}\r\n').encode()
        if language:
            body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                     f'name="language"\r\n\r\n{language}\r\n').encode()
        body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f'name="file"; filename="{filename}"\r\n'
                 f"Content-Type: {ctype}\r\n\r\n").encode()
        body += data
        body += f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(API_URL, data=bytes(body), method="POST")
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Content-Type",
                        f"multipart/form-data; boundary={boundary}")
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read())
        return (payload.get("text") or "").strip()

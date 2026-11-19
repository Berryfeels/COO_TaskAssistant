"""Unit tests for app/audio/mistral_stt.py: the Mistral (Voxtral) speech-to-
text client that backs the SimpleX gateway's transcriber hook. No network:
urllib.request.urlopen is stubbed; the multipart body is asserted."""
import json
import unittest
from unittest import mock

from app.audio.mistral_stt import API_URL, MistralSTT


class _Resp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestTranscribeRequest(unittest.TestCase):
    def test_multipart_request_shape_and_text_response(self):
        captured = {}

        def fake_urlopen(req, *a, **kw):
            captured["req"] = req
            captured["body"] = req.data
            return _Resp({"text": " ciao, aggiungi una card  "})

        stt = MistralSTT(api_key="k", model="voxtral-mini-latest")
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            text = stt.transcribe_bytes(b"OGG-AUDIO-BYTES", "voice.ogg")
        self.assertEqual(text, "ciao, aggiungi una card")   # stripped
        req = captured["req"]
        self.assertEqual(req.full_url, API_URL)
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"), "Bearer k")
        ctype = req.get_header("Content-type")
        self.assertIn("multipart/form-data", ctype)
        boundary = ctype.split("boundary=")[1]
        body = captured["body"]
        self.assertIn(f'name="model"\r\n\r\nvoxtral-mini-latest'.encode(), body)
        self.assertIn(b'name="response_format"', body)
        self.assertIn(b'name="file"; filename="voice.ogg"', body)
        self.assertIn(b"Content-Type: audio/ogg", body)
        self.assertIn(b"OGG-AUDIO-BYTES", body)
        self.assertTrue(body.rstrip().endswith(f"--{boundary}--".encode()))

    def test_language_is_sent_when_given(self):
        captured = {}

        def fake_urlopen(req, *a, **kw):
            captured["body"] = req.data
            return _Resp({"text": "ok"})

        stt = MistralSTT(api_key="k", model="m")
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            stt.transcribe_bytes(b"x", "a.wav", language="it")
        self.assertIn(b'name="language"\r\n\r\nit', captured["body"])

    def test_missing_api_key_raises_before_any_request(self):
        stt = MistralSTT(api_key="", model="m")
        with mock.patch("urllib.request.urlopen",
                        lambda *a, **kw: self.fail("must not call the API")):
            with self.assertRaises(RuntimeError):
                stt.transcribe_bytes(b"x")

    def test_transcribe_file_reads_from_disk(self):
        import os
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as fh:
            fh.write(b"DISK-AUDIO")
            path = fh.name
        try:
            captured = {}

            def fake_urlopen(req, *a, **kw):
                captured["body"] = req.data
                return _Resp({"text": "from disk"})

            stt = MistralSTT(api_key="k", model="m")
            with mock.patch("urllib.request.urlopen", fake_urlopen):
                text = stt.transcribe_file(path)
            self.assertEqual(text, "from disk")
            self.assertIn(b"DISK-AUDIO", captured["body"])
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()

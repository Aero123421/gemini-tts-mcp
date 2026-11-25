"""Offline tests using real FastMCP, ASGI, and Google SDK types."""

import importlib
import io
import json
import os
import unittest
import wave
import zipfile
from collections import OrderedDict
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

try:
    for dependency in ("starlette", "pydantic", "google.genai", "fastmcp"):
        importlib.import_module(dependency)
except ModuleNotFoundError as exc:
    if exc.name not in {"fastmcp", "google", "google.genai", "pydantic", "starlette"}:
        raise
    raise unittest.SkipTest(f"Missing integration dependency: {exc.name}") from exc

from cryptography.fernet import Fernet
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.google import GoogleProvider
from google.api_core.exceptions import FailedPrecondition, NotFound
from google.cloud import firestore
from key_value.aio.stores.memory import MemoryStore
from mcp.server.auth.provider import RefreshToken

from gemini_tts_mcp import server
from gemini_tts_mcp.auth import build_auth_provider, build_firestore_client_storage
from gemini_tts_mcp.config import Settings


def make_wav():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setparams((1, 2, 24000, 0, "NONE", ""))
        wav.writeframes(bytes(150_000))
    return buf.getvalue()


async def request(app, path):
    events = []

    async def send(event):
        events.append(event)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "scheme": "https",
            "method": "GET",
            "path": path,
            "query_string": b"",
            "headers": [],
            "root_path": "",
            "server": ("example.com", 443),
            "client": ("localhost", 12345),
        },
        receive,
        send,
    )
    start, *bodies = events
    return start["status"], dict(start["headers"]), [b["body"] for b in bodies]


class TestIntegration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        env = dict(
            BASE_URL="https://example.com",
            ALLOWED_EMAILS="test@example.com",
            GOOGLE_OAUTH_CLIENT_ID="test",
            GOOGLE_OAUTH_CLIENT_SECRET="test",
            GEMINI_API_KEY="test",
        )
        with patch.dict(os.environ, env, clear=True):
            self.cfg = Settings.from_env()
        self.storage = MemoryStore()
        self.enterContext(patch("gemini_tts_mcp.auth.build_firestore_client_storage", return_value=self.storage))
        claims = {"email": "test@example.com", "email_verified": "true"}
        self.enterContext(patch.object(server, "get_access_token", return_value=SimpleNamespace(claims=claims)))

    def test_parent_locks(self):
        provider = build_auth_provider(self.cfg)
        self.assertIsInstance(provider, GoogleProvider)
        self.assertIsInstance(provider._refresh_locks, OrderedDict)
        self.assertIsNot(provider._refresh_locks, provider._dual_policy_refresh_locks)

    async def test_atomic_firestore_delete(self):
        cfg = replace(self.cfg, gcp_project="test", storage_encryption_key=Fernet.generate_key().decode())
        with patch("key_value.aio.stores.firestore.store.firestore.AsyncClient") as factory:
            storage = build_firestore_client_storage(cfg)
        document = factory.return_value.collection.return_value.document.return_value
        document.delete = AsyncMock(side_effect=[None, NotFound("missing"), FailedPrecondition("already deleted")])
        for expected in (True, False, False):
            self.assertIs(await storage.delete(key="some/token", collection="test"), expected)
            self.assertEqual(document.delete.await_args.kwargs["option"], firestore.ExistsOption(exists=True))
        document.get.assert_not_called()

    async def test_async_sdk_calls_and_close(self):
        sdk = Mock()
        sdk.aio.aclose = AsyncMock()
        create = AsyncMock(return_value=SimpleNamespace(output_audio=SimpleNamespace(data="YQ==")))
        sdk.aio.interactions.create = create
        sdk.aio.voices.list = AsyncMock(return_value=SimpleNamespace(voices=[]))
        with patch("gemini_tts_mcp.tts.genai.Client", return_value=sdk):
            client = server.GeminiTTSClient(api_key="test")
        self.assertEqual(await client.synthesize_single("hello", "model"), b"a")
        self.assertEqual(await client.synthesize_dialogue([server.TurnSpec("hi")], "model"), b"a")
        self.assertEqual([call.kwargs["store"] for call in create.await_args_list], [False, False])
        self.assertEqual(await client.list_voices(), [])
        sdk.aio.voices.list.assert_awaited_once()
        await client.close()
        sdk.aio.aclose.assert_awaited_once()
        sdk.close.assert_called_once()

    async def test_stable_token_survives_provider_restart(self):
        client = SimpleNamespace(
            client_id="gemini", redirect_uris=["https://oauth-redirect.googleusercontent.com/r/app"]
        )
        current = "initial"

        async def load(client, token):
            if token == current:
                return RefreshToken(token=token, client_id=client.client_id, scopes=["openid"])
            return None

        self.enterContext(patch.object(GoogleProvider, "load_refresh_token", new=AsyncMock(side_effect=load)))
        exchange = AsyncMock(return_value=SimpleNamespace(refresh_token="rotated"))
        self.enterContext(patch.object(GoogleProvider, "exchange_refresh_token", exchange))
        provider = build_auth_provider(self.cfg)
        loaded = await provider.load_refresh_token(client, "initial")
        response = await provider.exchange_refresh_token(client, loaded, ["openid"])
        self.assertEqual(response.refresh_token, "initial")
        current = "rotated"
        restarted = build_auth_provider(self.cfg)
        loaded = await restarted.load_refresh_token(client, "initial")
        self.assertEqual(loaded.token, current)
        self.assertEqual(loaded._stable_refresh_token, "initial")

    async def test_dialogue_validation_before_rate_limit(self):
        mcp = server.create_server(replace(self.cfg, rate_block_per_min=1))
        app = mcp.http_app()
        synth = AsyncMock(return_value=make_wav())
        self.enterContext(patch.object(server.GeminiTTSClient, "synthesize_dialogue", synth))
        async with app.router.lifespan_context(app):
            invalid = [{"speaker": "A", "voice": "voice_custom", "text": "one"}, {"speaker": "B", "text": "two"}]
            with self.assertRaisesRegex(ToolError, "output_mode='split'"):
                await mcp.call_tool("generate_speech", {"segments": invalid})
            valid = [{"speaker": " A ", "text": " one "}, {"speaker": "A", "text": "two"}]
            await mcp.call_tool("generate_speech", {"segments": valid})
            turns = synth.await_args.kwargs["turns"]
            self.assertEqual([(turn.speaker, turn.text) for turn in turns], [("A", "one"), ("A", "two")])

    async def test_split_usage_on_assembly_failure(self):
        mcp = server.create_server(self.cfg)
        app = mcp.http_app()
        self.enterContext(patch.object(server.GeminiTTSClient, "synthesize_single", AsyncMock(return_value=make_wav())))
        self.enterContext(patch.object(server, "build_video_assets", side_effect=ValueError("assembly failed")))
        async with app.router.lifespan_context(app):
            result = await mcp.call_tool(
                "generate_speech", {"segments": [{"text": " x "}, {"text": "yy"}], "output_mode": "split"}
            )
            self.assertEqual(json.loads(result.content[0].text)["status"], "failed")
            usage = json.loads((await mcp.call_tool("get_usage", {})).content[0].text)
            self.assertEqual((usage["requests"], usage["input_chars"]), (2, 3))

    async def test_completed_job_routes(self):
        wav_bytes = make_wav()
        synth = AsyncMock(return_value=wav_bytes)
        self.enterContext(patch.object(server.GeminiTTSClient, "synthesize_single", synth))
        mcp = server.create_server(self.cfg)
        app = mcp.http_app()
        async with app.router.lifespan_context(app):
            for path, expected in (("/healthz", 200), ("/play/bad", 400), ("/play/" + "a" * 22, 404)):
                status, headers, _ = await request(app, path)
                self.assertEqual(status, expected)
                self.assertEqual(headers[b"cache-control"], b"no-store")
            result = await mcp.call_tool(
                "generate_speech", {"segments": [{"text": "  A sentence.  ", "id": "intro"}], "output_mode": "split"}
            )
            synth.assert_awaited_once()
            self.assertEqual(synth.await_args.kwargs["text"], "A sentence.")
            job = json.loads(result.content[0].text)
            jid = job["job_id"]
            for path in (
                f"/play/{jid}",
                f"/audio/{jid}/combined.wav",
                f"/audio/{jid}/scenes/001_intro.wav",
                f"/dl/{jid}.zip",
            ):
                status, headers, chunks = await request(app, path)
                self.assertEqual(status, 200)
                self.assertNotIn(b"content-length", headers)
                self.assertEqual(headers[b"x-content-type-options"], b"nosniff")
                self.assertEqual(headers[b"referrer-policy"], b"no-referrer")
                self.assertEqual(headers[b"cache-control"], b"no-store")
                self.assertLessEqual(max(map(len, chunks)), 65536)
                body = b"".join(chunks)
                if path.endswith(".wav"):
                    self.assertEqual(body, wav_bytes)
                elif path.endswith(".zip"):
                    with zipfile.ZipFile(io.BytesIO(body)) as archive:
                        manifest = json.loads(archive.read("narration/manifest.json"))
                        self.assertEqual(manifest["scenes"][0]["text"], "A sentence.")
                else:
                    self.assertIn(b"content-security-policy", headers)
                    self.assertIn(f"/audio/{jid}/combined.wav".encode(), body)

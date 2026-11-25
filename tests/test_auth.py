"""Unit tests for auth helpers, strict Gemini URI detection, Firestore sanitizer, and refresh policy."""

from __future__ import annotations

import asyncio
import gc
import hashlib
import types
import unittest
from collections import OrderedDict

from gemini_tts_mcp.auth import (
    DualPolicyRefreshMixin,
    authorize_user_claims,
    is_gemini_connected_app_client,
    sanitize_firestore_key_value,
)


class _MemStore:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def put(self, key: str, value: str) -> None:
        self.data[key] = value


class _FakeBaseProvider:
    def __init__(self) -> None:
        self._refresh_token_store = _MemStore()
        self._jti_mapping_store = _MemStore()
        self.exchange_calls = 0
        self.load_calls = 0
        self._refresh_locks = OrderedDict()

    def _hash_token(self, token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    async def load_refresh_token(self, client: object, refresh_token: str) -> object | None:
        self.load_calls += 1
        key = self._hash_token(refresh_token)
        if await self._refresh_token_store.get(key=key) is not None:
            return types.SimpleNamespace(token=refresh_token)
        return None

    async def exchange_refresh_token(
        self, client: object, refresh_token: types.SimpleNamespace, scopes: list[str]
    ) -> object:
        await asyncio.sleep(0.02)
        self.exchange_calls += 1
        old_tok = refresh_token.token
        old_key = self._hash_token(old_tok)
        if self._refresh_token_store.data.pop(old_key, None) is None:
            raise ValueError("Token already rotated")
        self._jti_mapping_store.data.pop(old_key, None)

        new_tok = f"rt_rotated_{self.exchange_calls}"
        new_key = self._hash_token(new_tok)
        await self._refresh_token_store.put(new_key, f"payload_{self.exchange_calls}")
        await self._jti_mapping_store.put(new_key, f"jti_{self.exchange_calls}")
        return types.SimpleNamespace(
            access_token=f"at_{self.exchange_calls}",
            refresh_token=new_tok,
        )


class _TestDualProvider(DualPolicyRefreshMixin, _FakeBaseProvider):
    def __init__(self) -> None:
        super().__init__()
        self._init_refresh_policy()


class TestAuthModule(unittest.IsolatedAsyncioTestCase):
    def test_strict_gemini_redirect_detection(self) -> None:
        self.assertTrue(
            is_gemini_connected_app_client(
                ["https://oauth-redirect.googleusercontent.com/r/my-project"]
            )
        )
        self.assertTrue(
            is_gemini_connected_app_client(
                [
                    "https://oauth-redirect.googleusercontent.com/r/p1",
                    "https://oauth-redirect-sandbox.googleusercontent.com/a/p2",
                ]
            )
        )
        # Mixed attacker URI + Gemini URI must NOT qualify
        self.assertFalse(
            is_gemini_connected_app_client(
                [
                    "https://oauth-redirect.googleusercontent.com/r/p1",
                    "https://evil.example.com/callback",
                ]
            )
        )
        # Wrong scheme or wrong path must NOT qualify
        self.assertFalse(
            is_gemini_connected_app_client(
                ["http://oauth-redirect.googleusercontent.com/r/p1"]
            )
        )
        self.assertFalse(
            is_gemini_connected_app_client(
                ["https://oauth-redirect.googleusercontent.com/other/p1"]
            )
        )
        self.assertFalse(
            is_gemini_connected_app_client(["https://claude.ai/api/mcp/auth_callback"])
        )

    def test_firestore_key_sanitization_covers_all_firestore_constraints(self) -> None:
        tricky_keys = [
            "",
            ".",
            "..",
            "__reserved__",
            "H_abc123",
            "S_xyz789",
            "K_existing_prefix",
            "https://chatgpt.com/oauth/client.json",
            "a" * 2000,
        ]
        outputs = set()
        for k in tricky_keys:
            s = sanitize_firestore_key_value(k)
            self.assertTrue(s.startswith("K_"))
            self.assertNotIn("/", s)
            self.assertLessEqual(len(s.encode("utf-8")), 100)
            outputs.add(s)
        # All distinct inputs produce distinct outputs (injective)
        self.assertEqual(len(outputs), len(tricky_keys))

    async def test_gemini_consecutive_refreshes_preserve_token_in_hashed_store(self) -> None:
        provider = _TestDualProvider()
        gemini_client = types.SimpleNamespace(
            client_id="gemini-dcr-1",
            redirect_uris=["https://oauth-redirect.googleusercontent.com/r/app1"],
        )
        orig_rt = "rt_initial_gemini"
        orig_key = provider._hash_token(orig_rt)
        await provider._refresh_token_store.put(orig_key, "init_payload")
        await provider._jti_mapping_store.put(orig_key, "init_jti")

        # 1st refresh
        loaded1 = await provider.load_refresh_token(gemini_client, orig_rt)
        self.assertIsNotNone(loaded1)
        res1 = await provider.exchange_refresh_token(gemini_client, loaded1, ["openid"])
        self.assertEqual(res1.refresh_token, orig_rt)

        # 2nd consecutive refresh with the SAME token must still succeed
        loaded2 = await provider.load_refresh_token(gemini_client, orig_rt)
        self.assertEqual(loaded2.token, "rt_rotated_1")
        self.assertNotIn(orig_key, provider._refresh_token_store.data)
        res2 = await provider.exchange_refresh_token(gemini_client, loaded2, ["openid"])
        self.assertEqual(res2.refresh_token, orig_rt)
        self.assertEqual(provider.exchange_calls, 2)
        loaded = await provider.load_refresh_token(gemini_client, orig_rt)
        await asyncio.gather(
            provider.exchange_refresh_token(gemini_client, loaded, ["openid"]),
            provider.exchange_refresh_token(gemini_client, loaded, ["openid"]),
        )
        provider._refresh_token_store.data.clear()
        self.assertIsNone(await provider.load_refresh_token(gemini_client, orig_rt))
        self.assertFalse(provider._stable_refresh_fallback)

    async def test_claude_concurrent_refresh_single_flight_and_grace_load(self) -> None:
        provider = _TestDualProvider()
        claude_client = types.SimpleNamespace(
            client_id="claude-client-1",
            redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
        )
        orig_rt = "rt_initial_claude"
        await provider._refresh_token_store.put(provider._hash_token(orig_rt), "init")

        rt_obj = await provider.load_refresh_token(claude_client, orig_rt)
        # Fire two concurrent refreshes with the same token
        r1, r2 = await asyncio.gather(
            provider.exchange_refresh_token(claude_client, rt_obj, ["openid"]),
            provider.exchange_refresh_token(claude_client, rt_obj, ["openid"]),
        )
        # Single-flight lock ensures upstream exchange ran only once and both got same response
        self.assertEqual(provider.exchange_calls, 1)
        self.assertEqual(r1.refresh_token, r2.refresh_token)
        self.assertNotEqual(r1.refresh_token, orig_rt)

        # Subsequent retry within 30s grace window passes load_refresh_token too
        grace_loaded = await provider.load_refresh_token(claude_client, orig_rt)
        self.assertIs(rt_obj, grace_loaded)
        self.assertEqual(provider.load_calls, 1)
        r3 = await provider.exchange_refresh_token(claude_client, grace_loaded, ["openid"])
        self.assertEqual(r3.refresh_token, r1.refresh_token)
        self.assertEqual(provider.exchange_calls, 1)

        # Different scopes must NOT reuse the cached token response
        with self.assertRaisesRegex(ValueError, "already rotated"):
            await provider.exchange_refresh_token(claude_client, grace_loaded, ["openid", "email"])
        self.assertEqual(provider.exchange_calls, 2)

    async def test_refresh_locks_do_not_leak_for_gemini_or_failed_refreshes(self) -> None:
        provider = _TestDualProvider()
        gemini_client = types.SimpleNamespace(
            client_id="gemini-dcr-leak",
            redirect_uris=["https://oauth-redirect.googleusercontent.com/r/app1"],
        )
        for i in range(10):
            rt = f"rt_gemini_{i}"
            await provider._refresh_token_store.put(provider._hash_token(rt), "payload")
            loaded = await provider.load_refresh_token(gemini_client, rt)
            await provider.exchange_refresh_token(gemini_client, loaded, ["openid"])

        gc.collect()
        self.assertIsInstance(provider._refresh_locks, OrderedDict)
        self.assertEqual(len(provider._dual_policy_refresh_locks), 0)

    def test_authorize_user_claims_fail_closed(self) -> None:
        allowed = frozenset({"alice@example.com"})
        self.assertEqual(
            authorize_user_claims({"email": "Alice@Example.com", "email_verified": True}, allowed),
            "alice@example.com",
        )
        self.assertEqual(authorize_user_claims({"email": "alice@example.com", "email_verified": "true"}, allowed),
                         "alice@example.com")
        for bad_claims in (
            None,
            {},
            {"email": "alice@example.com"},
            {"email": "alice@example.com", "email_verified": None},
            {"email": "alice@example.com", "email_verified": False},
            *({"email": "alice@example.com", "email_verified": value}
              for value in ("false", "TRUE", " true", 1, 0, [], {})),
            {"email": "bob@example.com", "email_verified": True},
        ):
            with self.assertRaises(PermissionError):
                authorize_user_claims(bad_claims, allowed)

        # allow_any_user still requires verified email
        self.assertEqual(
            authorize_user_claims(
                {"email": "bob@example.com", "email_verified": True},
                allowed,
                allow_any_user=True,
            ),
            "bob@example.com",
        )
        with self.assertRaises(PermissionError):
            authorize_user_claims(
                {"email": "bob@example.com"},
                allowed,
                allow_any_user=True,
            )


if __name__ == "__main__":
    unittest.main()

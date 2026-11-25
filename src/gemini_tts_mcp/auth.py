"""OAuth 2.1 authentication compatible with ChatGPT Web, Claude.ai, and Gemini.

Fixes verified 2026-11 compatibility and security edge cases:
1. Strict Gemini Connected Apps detection (all redirect_uris must be https + allowed host + /r/ or /a/).
2. Single-flight asyncio.Lock + load_refresh_token grace cache for concurrent refresh races (FastMCP #4901).
3. Gemini non-rotating refresh token handling using a stable alias for the current rotated token.
4. Injectively hashed Firestore keys (K_<sha256>) avoiding ALL Firestore doc ID constraints
   ('.', '..', '__.*__', '/', >1500 bytes, and py-key-value-aio H_/S_ bug #136).
5. Fernet encryption wrapper with raise_on_decryption_error=False.
6. Optional MultiAuth + GoogleTokenVerifier for Gemini Enterprise static OAuth clients.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import weakref
from collections import OrderedDict
from typing import Any
from urllib.parse import urlparse

from .config import Settings

logger = logging.getLogger(__name__)

GEMINI_REDIRECT_HOSTS = frozenset(
    {
        "oauth-redirect.googleusercontent.com",
        "oauth-redirect-sandbox.googleusercontent.com",
        "oauth-redirect-test.googleusercontent.com",
    }
)
GEMINI_REDIRECT_PATH_PREFIXES = ("/r/", "/a/")


def is_gemini_connected_app_client(redirect_uris: list[str] | tuple[str, ...] | None) -> bool:
    """Return True iff ALL registered redirect URIs match Gemini Connected Apps Platform."""
    if not redirect_uris:
        return False
    for uri in redirect_uris:
        try:
            parsed = urlparse(str(uri))
        except Exception:
            return False
        if parsed.scheme != "https":
            return False
        host = (parsed.hostname or "").lower()
        if host not in GEMINI_REDIRECT_HOSTS:
            return False
        if not parsed.path.startswith(GEMINI_REDIRECT_PATH_PREFIXES):
            return False
    return True


def authorize_user_claims(
    claims: dict[str, Any] | None,
    allowed_emails: frozenset[str],
    allow_any_user: bool = False,
) -> str:
    """Fail-closed user authorization requiring a verified email claim."""
    if not claims:
        raise PermissionError("Authenticated email claim is required.")
    email = str(claims.get("email") or "").strip().lower()
    if not email:
        raise PermissionError("Authenticated email claim is required.")
    verified = claims.get("email_verified")
    if verified is not True and not (isinstance(verified, str) and verified == "true"):
        raise PermissionError("Verified email claim is required.")
    if not allow_any_user and email not in allowed_emails:
        raise PermissionError("Access denied for this account.")
    return email


def sanitize_firestore_key_value(value: str) -> str:
    """Injectively map ANY key to a valid Firestore doc ID (K_<sha256>).

    Avoids '/', '.', '..', '__.*__', >1500 UTF-8 bytes, empty strings, and the
    py-key-value-aio H_/S_ reserved prefix bug (googleads/google-ads-mcp #136)
    with zero collision against raw keys.
    """
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"K_{digest}"


def build_firestore_client_storage(settings: Settings) -> Any:
    """Create encrypted Firestore client_storage for FastMCP OAuthProxy on Cloud Run."""
    if not settings.gcp_project:
        logger.warning(
            "GOOGLE_CLOUD_PROJECT is not set; using default in-memory/local FastMCP storage "
            "(NOT suitable for Cloud Run scale-to-zero)."
        )
        return None

    if not settings.storage_encryption_key:
        raise ValueError(
            "STORAGE_ENCRYPTION_KEY (Fernet key) is required when Firestore client_storage is enabled."
        )

    from cryptography.fernet import Fernet
    from google.api_core.exceptions import FailedPrecondition, NotFound
    from google.cloud import firestore
    from key_value.aio.stores.firestore import (
        FirestoreStore,
        FirestoreV1CollectionSanitizationStrategy,
        FirestoreV1KeySanitizationStrategy,
    )
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
    from key_value.aio.wrappers.prefix_collections import PrefixCollectionsWrapper

    class SafeFirestoreKeySanitizationStrategy(FirestoreV1KeySanitizationStrategy):
        """Uniform K_<sha256> key sanitizer avoiding all Firestore & py-key-value-aio #136 errors."""

        def validate(self, value: str) -> None:
            return None

        def sanitize(self, value: str) -> str:
            return sanitize_firestore_key_value(value)

    class AtomicFirestoreStore(FirestoreStore):
        async def _delete_managed_entry(self, *, key: str, collection: str | None = None) -> bool:
            collection, key = self._sanitize_collection_and_key(
                collection=collection or self.default_collection, key=key
            )
            doc_ref = self._client.collection(collection).document(key)
            try:
                await doc_ref.delete(option=firestore.ExistsOption(exists=True))
            except (NotFound, FailedPrecondition):
                return False
            return True

    store = AtomicFirestoreStore(
        project=settings.gcp_project,
        database=settings.firestore_database,
        key_sanitization_strategy=SafeFirestoreKeySanitizationStrategy(),
        collection_sanitization_strategy=FirestoreV1CollectionSanitizationStrategy(),
    )
    prefixed = PrefixCollectionsWrapper(
        key_value=store,
        prefix=settings.firestore_prefix,
    )
    return FernetEncryptionWrapper(
        key_value=prefixed,
        fernet=Fernet(settings.storage_encryption_key.encode("utf-8")),
        raise_on_decryption_error=False,
    )


class DualPolicyRefreshMixin:
    """Reusable mixin implementing Gemini non-rotation + single-flight grace period for refresh tokens."""

    _grace_seconds: float = 30.0
    _max_grace_entries: int = 1000
    _stable_collection = "gemini_stable_refresh"

    def _init_refresh_policy(self) -> None:
        # (client_id, sha256(old_token)) -> (expiry_monotonic, old_refresh_obj, normalized_scopes, oauth_token_response)
        self._refresh_grace_cache: OrderedDict[
            tuple[str, str], tuple[float, Any, tuple[str, ...], Any]
        ] = OrderedDict()
        self._stable_storage = getattr(self, "_client_storage", getattr(self, "client_storage", None))
        self._stable_refresh_fallback: dict[str, str] = {}
        # Weak references release idle per-token locks.
        self._dual_policy_refresh_locks: weakref.WeakValueDictionary[tuple[str, str], asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    @staticmethod
    def _cache_key(client: Any, token_str: str) -> tuple[str, str]:
        client_id = str(getattr(client, "client_id", ""))
        token_hash = hashlib.sha256(token_str.encode("utf-8")).hexdigest()
        return (client_id, token_hash)

    @staticmethod
    def _normalize_scopes(scopes: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
        return tuple(sorted(str(s) for s in (scopes or ())))

    def _prune_grace_cache(self, now: float) -> None:
        expired = [k for k, (exp, _, _, _) in self._refresh_grace_cache.items() if exp <= now]
        for k in expired:
            self._refresh_grace_cache.pop(k, None)
        while len(self._refresh_grace_cache) > self._max_grace_entries:
            self._refresh_grace_cache.popitem(last=False)

    def _policy_lock(self, key: tuple[str, str]) -> asyncio.Lock:
        lock = self._dual_policy_refresh_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._dual_policy_refresh_locks[key] = lock
        return lock

    async def _mapped_token(self, key: str) -> str | None:
        storage = self._stable_storage
        if storage is None:
            return self._stable_refresh_fallback.get(key)
        entry = await storage.get(key=key, collection=self._stable_collection)
        return entry["token"] if entry is not None else None

    async def _map_token(self, key: str, token: str | None) -> None:
        storage = self._stable_storage
        if storage is None:
            if token is None:
                self._stable_refresh_fallback.pop(key, None)
            else:
                self._stable_refresh_fallback[key] = token
        elif token is None:
            await storage.delete(key=key, collection=self._stable_collection)
        else:
            await storage.put(key=key, value={"token": token}, collection=self._stable_collection)

    async def load_refresh_token(self, client: Any, refresh_token: str) -> Any:
        key = self._cache_key(client, refresh_token)
        async with self._policy_lock(key):
            if is_gemini_connected_app_client(getattr(client, "redirect_uris", None)):
                storage_key = ":".join(key)
                mapped = await self._mapped_token(storage_key)
                loaded = await super().load_refresh_token(client, mapped or refresh_token)
                if mapped and loaded is None:
                    await self._map_token(storage_key, None)
                elif loaded is not None:
                    loaded._stable_refresh_token = refresh_token
                return loaded
            self._prune_grace_cache(time.monotonic())
            entry = self._refresh_grace_cache.get(key)
            if entry is not None:
                return entry[1]
            return await super().load_refresh_token(client, refresh_token)

    async def exchange_refresh_token(self, client: Any, refresh_token: Any, scopes: list[str]) -> Any:
        stable = getattr(refresh_token, "_stable_refresh_token", refresh_token.token)
        key = self._cache_key(client, stable)
        async with self._policy_lock(key):
            if is_gemini_connected_app_client(getattr(client, "redirect_uris", None)):
                storage_key = ":".join(key)
                # Another exchange may have rotated the token since the handler loaded it.
                mapped = await self._mapped_token(storage_key)
                if mapped and mapped != refresh_token.token:
                    refresh_token = await super().load_refresh_token(client, mapped)
                    if refresh_token is None:
                        await self._map_token(storage_key, None)
                        from mcp.server.auth.provider import TokenError

                        raise TokenError("invalid_grant", "Refresh token has expired")
                response = await super().exchange_refresh_token(client, refresh_token, scopes)
                if response.refresh_token:
                    await self._map_token(storage_key, response.refresh_token)
                    response.refresh_token = stable
                return response

            self._prune_grace_cache(time.monotonic())
            entry = self._refresh_grace_cache.get(key)
            norm_scopes = self._normalize_scopes(scopes)
            if entry is not None and entry[2] == norm_scopes:
                return entry[3]
            response = await super().exchange_refresh_token(client, refresh_token, scopes)
            self._refresh_grace_cache[key] = (
                time.monotonic() + self._grace_seconds, refresh_token, norm_scopes, response
            )
            return response


def build_auth_provider(settings: Settings) -> Any:
    """Build DualPolicyGoogleProvider (optionally wrapped in MultiAuth for Gemini Enterprise)."""
    from fastmcp.server.auth.providers.google import GoogleProvider

    client_storage = build_firestore_client_storage(settings)

    class DualPolicyGoogleProvider(DualPolicyRefreshMixin, GoogleProvider):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._init_refresh_policy()

    provider_kwargs: dict[str, Any] = {
        "client_id": settings.google_oauth_client_id,
        "client_secret": settings.google_oauth_client_secret,
        "base_url": settings.base_url,
        "required_scopes": [
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
        ],
        "cache_ttl_seconds": 300,
    }
    if settings.jwt_signing_key:
        provider_kwargs["jwt_signing_key"] = settings.jwt_signing_key
    if client_storage is not None:
        provider_kwargs["client_storage"] = client_storage

    primary = DualPolicyGoogleProvider(**provider_kwargs)

    if settings.gemini_enterprise_client_id:
        from fastmcp.server.auth import MultiAuth
        from fastmcp.server.auth.providers.google import GoogleTokenVerifier

        enterprise_verifier = GoogleTokenVerifier(
            audience=settings.gemini_enterprise_client_id,
            cache_ttl_seconds=300,
        )
        return MultiAuth(server=primary, verifiers=[enterprise_verifier])

    return primary

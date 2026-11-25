"""OAuth 2.1 authentication compatible with ChatGPT Web, Claude.ai, and Gemini.

Fixes verified 2026-11 compatibility and security edge cases:
1. Strict Gemini Connected Apps detection (all redirect_uris must be https + allowed host + /r/ or /a/).
2. Single-flight asyncio.Lock + load_refresh_token grace cache for concurrent refresh races (FastMCP #4901).
3. Gemini non-rotating refresh token handling supporting both hashed and raw FastMCP store keys.
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
    if claims.get("email_verified") is not True:
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

    store = FirestoreStore(
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

    def _init_refresh_policy(self) -> None:
        # (client_id, sha256(old_token)) -> (expiry_monotonic, old_refresh_obj, normalized_scopes, oauth_token_response)
        self._refresh_grace_cache: OrderedDict[
            tuple[str, str], tuple[float, Any, tuple[str, ...], Any]
        ] = OrderedDict()
        # (client_id, sha256(token)) -> asyncio.Lock (weak-referenced so unused/failed locks never leak)
        self._refresh_locks: weakref.WeakValueDictionary[tuple[str, str], asyncio.Lock] = (
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

    def _store_key_for_token(self, token_str: str) -> str:
        """Match FastMCP's internal token hashing if present."""
        hash_fn = getattr(self, "_hash_token", None)
        if callable(hash_fn):
            return str(hash_fn(token_str))
        return token_str

    async def load_refresh_token(self, client: Any, refresh_token: str) -> Any:
        loaded = await super().load_refresh_token(client, refresh_token)  # type: ignore[misc]
        if loaded is not None:
            return loaded
        now = time.monotonic()
        self._prune_grace_cache(now)
        ckey = self._cache_key(client, refresh_token)
        entry = self._refresh_grace_cache.get(ckey)
        if entry is not None and entry[0] > now:
            return entry[1]
        return None

    async def exchange_refresh_token(
        self,
        client: Any,
        refresh_token: Any,
        scopes: list[str],
    ) -> Any:
        token_str = getattr(refresh_token, "token", str(refresh_token))
        ckey = self._cache_key(client, token_str)
        norm_scopes = self._normalize_scopes(scopes)
        lock = self._refresh_locks.get(ckey)
        if lock is None:
            lock = asyncio.Lock()
            self._refresh_locks[ckey] = lock

        async with lock:
            now = time.monotonic()
            self._prune_grace_cache(now)

            entry = self._refresh_grace_cache.get(ckey)
            if entry is not None and entry[0] > now and entry[2] == norm_scopes:
                return entry[3]

            redirect_uris = getattr(client, "redirect_uris", None) or []
            is_gemini = is_gemini_connected_app_client([str(u) for u in redirect_uris])

            oauth_token = await super().exchange_refresh_token(client, refresh_token, scopes)  # type: ignore[misc]

            if is_gemini:
                new_rt = getattr(oauth_token, "refresh_token", None)
                if new_rt and new_rt != token_str:
                    await self._restore_gemini_refresh_token(old_rt=token_str, new_rt=new_rt)
                    oauth_token.refresh_token = token_str
            else:
                self._refresh_grace_cache[ckey] = (
                    now + self._grace_seconds,
                    refresh_token,
                    norm_scopes,
                    oauth_token,
                )
                self._refresh_grace_cache.move_to_end(ckey)

            return oauth_token

    async def _restore_gemini_refresh_token(self, old_rt: str, new_rt: str) -> None:
        """Preserve the original refresh token in FastMCP's backing stores for Gemini Persistent Linking."""
        old_key = self._store_key_for_token(old_rt)
        new_key = self._store_key_for_token(new_rt)

        rt_store = getattr(self, "_refresh_token_store", None)
        if rt_store is not None:
            stored = await rt_store.get(key=new_key)
            if stored is None and new_key != new_rt:
                stored = await rt_store.get(key=new_rt)
                old_key = old_rt
            if stored is not None:
                await rt_store.put(key=old_key, value=stored)
            else:
                logger.warning(
                    "FastMCP _refresh_token_store did not contain new refresh token; "
                    "Gemini non-rotation fallback may require updating for this FastMCP version."
                )

        jti_store = getattr(self, "_jti_mapping_store", None)
        if jti_store is not None:
            jti_val = await jti_store.get(key=new_key)
            if jti_val is not None:
                await jti_store.put(key=old_key, value=jti_val)


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

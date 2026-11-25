"""Configuration and pricing constants for Gemini TTS MCP."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timezone

MODEL_MAP: dict[str, str] = {
    "fast": "gemini-3.8-flash-lite-tts",
    "studio": "gemini-3.8-flash-tts",
}

# Standard Paid Tier pricing (USD per 1M tokens)
# Ref: https://aistudio.google.com/docs/models/gemini-3.8-flash-lite-tts
# Ref: https://aistudio.google.com/docs/models/gemini-3.8-flash-tts
PRICING_2026: dict[str, dict[str, float]] = {
    "gemini-3.8-flash-lite-tts": {"input_per_1m": 0.50, "audio_per_1m": 6.00},
    "gemini-3.8-flash-tts": {"input_per_1m": 0.75, "audio_per_1m": 9.00},
}
PRICING_2027: dict[str, dict[str, float]] = {
    "gemini-3.8-flash-lite-tts": {"input_per_1m": 1.00, "audio_per_1m": 12.00},
    "gemini-3.8-flash-tts": {"input_per_1m": 1.50, "audio_per_1m": 18.00},
}

# Conservative estimate matching empirical Japanese billing (~32 tok/s vs official 25 tok/s)
AUDIO_TOKENS_PER_SEC_ESTIMATE = 32.0


def estimate_cost_usd(
    model: str,
    input_chars: int,
    duration_sec: float,
    today: date | None = None,
) -> tuple[int, float]:
    """Return (estimated_audio_tokens, estimated_cost_usd) using UTC date."""
    ref_day = today or datetime.now(timezone.utc).date()
    table = PRICING_2027 if ref_day >= date(2027, 1, 1) else PRICING_2026
    rates = table.get(model, table["gemini-3.8-flash-lite-tts"])
    est_input_tokens = max(1, input_chars)
    est_audio_tokens = int(round(duration_sec * AUDIO_TOKENS_PER_SEC_ESTIMATE))
    cost = (
        (est_input_tokens / 1_000_000) * rates["input_per_1m"]
        + (est_audio_tokens / 1_000_000) * rates["audio_per_1m"]
    )
    return est_audio_tokens, round(cost, 6)


@dataclass(frozen=True)
class Settings:
    base_url: str
    gemini_api_key: str
    google_oauth_client_id: str
    google_oauth_client_secret: str
    allowed_emails: frozenset[str]
    allow_any_user: bool
    jwt_signing_key: str | None
    storage_encryption_key: str | None
    gcp_project: str | None
    firestore_database: str
    firestore_prefix: str
    gemini_enterprise_client_id: str | None
    rate_warn_per_min: int
    rate_block_per_min: int
    rate_warn_per_hour: int
    rate_block_per_hour: int
    max_chars_per_segment: int
    max_total_chars: int
    max_segments: int
    tts_concurrency: int
    tts_timeout_sec: float
    sync_wait_sec: float
    poll_wait_sec: float
    max_cached_jobs: int
    max_cache_bytes: int
    job_ttl_sec: float
    monthly_budget_usd: float
    port: int

    @classmethod
    def from_env(cls) -> "Settings":
        allowed = frozenset(
            e.strip().lower()
            for e in os.environ.get("ALLOWED_EMAILS", "").split(",")
            if e.strip()
        )
        allow_any_user = os.environ.get("ALLOW_ANY_USER", "").strip().lower() == "true"
        if not allowed and not allow_any_user:
            raise ValueError(
                "ALLOWED_EMAILS must be set (fail-closed). "
                "To intentionally allow any authenticated Google account, set ALLOW_ANY_USER=true."
            )

        base_url = os.environ["BASE_URL"].rstrip("/")
        gcp_project = os.environ.get("GOOGLE_CLOUD_PROJECT") or None
        jwt_signing_key = os.environ.get("JWT_SIGNING_KEY") or None
        storage_encryption_key = os.environ.get("STORAGE_ENCRYPTION_KEY") or None

        # Fail-fast on Cloud Run (K_SERVICE is automatically injected by Cloud Run)
        if os.environ.get("K_SERVICE"):
            if not base_url.startswith("https://"):
                raise ValueError("BASE_URL must start with https:// on Cloud Run.")
            if not gcp_project:
                raise ValueError("GOOGLE_CLOUD_PROJECT is required on Cloud Run for Firestore persistence.")
            if not jwt_signing_key:
                raise ValueError("JWT_SIGNING_KEY is required on Cloud Run.")
            if not storage_encryption_key:
                raise ValueError("STORAGE_ENCRYPTION_KEY is required on Cloud Run.")

        max_segments = int(os.environ.get("MAX_SEGMENTS", "20"))
        raw_block_per_min = int(os.environ.get("RATE_BLOCK_PER_MIN", "30"))
        # Guarantee a single valid split batch is never unconditionally blocked
        rate_block_per_min = max(raw_block_per_min, max_segments)
        rate_warn_per_min = min(
            int(os.environ.get("RATE_WARN_PER_MIN", "10")),
            rate_block_per_min,
        )
        rate_block_per_hour = max(
            int(os.environ.get("RATE_BLOCK_PER_HOUR", "120")),
            rate_block_per_min,
        )
        rate_warn_per_hour = min(
            int(os.environ.get("RATE_WARN_PER_HOUR", "60")),
            rate_block_per_hour,
        )

        return cls(
            base_url=base_url,
            gemini_api_key=os.environ["GEMINI_API_KEY"],
            google_oauth_client_id=os.environ["GOOGLE_OAUTH_CLIENT_ID"],
            google_oauth_client_secret=os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
            allowed_emails=allowed,
            allow_any_user=allow_any_user,
            jwt_signing_key=jwt_signing_key,
            storage_encryption_key=storage_encryption_key,
            gcp_project=gcp_project,
            firestore_database=os.environ.get("FIRESTORE_DATABASE", "(default)"),
            firestore_prefix=os.environ.get(
                "FIRESTORE_COLLECTION_PREFIX", "gemini-tts-mcp"
            ),
            gemini_enterprise_client_id=os.environ.get(
                "GEMINI_ENTERPRISE_CLIENT_ID"
            )
            or None,
            rate_warn_per_min=rate_warn_per_min,
            rate_block_per_min=rate_block_per_min,
            rate_warn_per_hour=rate_warn_per_hour,
            rate_block_per_hour=rate_block_per_hour,
            max_chars_per_segment=int(os.environ.get("MAX_CHARS_PER_SEGMENT", "4000")),
            max_total_chars=int(os.environ.get("MAX_TOTAL_CHARS", "20000")),
            max_segments=max_segments,
            tts_concurrency=int(os.environ.get("TTS_CONCURRENCY", "4")),
            tts_timeout_sec=float(os.environ.get("TTS_TIMEOUT_SEC", "60")),
            sync_wait_sec=float(os.environ.get("SYNC_WAIT_SEC", "25")),
            poll_wait_sec=float(os.environ.get("POLL_WAIT_SEC", "25")),
            max_cached_jobs=int(os.environ.get("MAX_CACHED_JOBS", "20")),
            max_cache_bytes=int(os.environ.get("MAX_CACHE_BYTES", str(256 * 1024 * 1024))),
            job_ttl_sec=float(os.environ.get("JOB_TTL_SEC", "900")),
            monthly_budget_usd=float(os.environ.get("MONTHLY_BUDGET_USD", "100.0")),
            port=int(os.environ.get("PORT", "8080")),
        )

"""Real-time monthly usage tracker backed by Firestore (with in-memory fallback)."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Any

from .config import MODEL_MAP, estimate_cost_usd

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_MODEL_BUCKET: dict[str, str] = {
    MODEL_MAP["fast"]: "flash_lite",
    MODEL_MAP["studio"]: "flash",
}


def _current_month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def validate_month_key(month: str) -> str:
    """Validate YYYY-MM format to prevent Firestore path traversal / invalid doc IDs."""
    cleaned = month.strip()
    if not _MONTH_RE.fullmatch(cleaned):
        raise ValueError(f"Invalid month format '{month}'. Expected YYYY-MM (e.g. '2026-11').")
    return cleaned


class UsageTracker:
    """Tracks per-month TTS generation counts, audio seconds, and estimated USD spend."""

    def __init__(
        self,
        monthly_budget_usd: float = 100.0,
        gcp_project: str | None = None,
        database: str = "(default)",
        collection_prefix: str = "gemini-tts-mcp",
    ) -> None:
        self.monthly_budget_usd = monthly_budget_usd
        self._collection_name = f"{collection_prefix}_usage"
        self._db: Any = None
        self._mem: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

        if gcp_project:
            from google.cloud import firestore

            self._firestore_mod = firestore
            self._db = firestore.AsyncClient(project=gcp_project, database=database)

    async def close(self) -> None:
        if self._db is not None and hasattr(self._db, "close"):
            res = self._db.close()
            if asyncio.iscoroutine(res):
                await res

    async def record(
        self,
        model: str,
        input_chars: int,
        duration_sec: float,
        requests: int = 1,
        user_email: str = "",
    ) -> dict[str, Any]:
        """Atomically increment current month's usage counters."""
        month = _current_month_key()
        est_audio_tokens, est_cost_usd = estimate_cost_usd(
            model=model,
            input_chars=input_chars,
            duration_sec=duration_sec,
        )
        model_key = _MODEL_BUCKET.get(model, "other")

        if self._db is not None:
            doc_ref = self._db.collection(self._collection_name).document(month)
            inc = self._firestore_mod.Increment
            await doc_ref.set(
                {
                    "month": month,
                    "requests": inc(requests),
                    "input_chars": inc(input_chars),
                    "audio_seconds": inc(duration_sec),
                    "estimated_audio_tokens": inc(est_audio_tokens),
                    "estimated_cost_usd": inc(est_cost_usd),
                    f"by_model_{model_key}_requests": inc(requests),
                    f"by_model_{model_key}_seconds": inc(duration_sec),
                    f"by_model_{model_key}_cost_usd": inc(est_cost_usd),
                    "last_updated_utc": datetime.now(timezone.utc).isoformat(),
                    "last_user": user_email,
                },
                merge=True,
            )
        else:
            async with self._lock:
                entry = self._mem.setdefault(
                    month,
                    {
                        "month": month,
                        "requests": 0,
                        "input_chars": 0,
                        "audio_seconds": 0.0,
                        "estimated_audio_tokens": 0,
                        "estimated_cost_usd": 0.0,
                    },
                )
                entry["requests"] += requests
                entry["input_chars"] += input_chars
                entry["audio_seconds"] += duration_sec
                entry["estimated_audio_tokens"] += est_audio_tokens
                entry["estimated_cost_usd"] += est_cost_usd
                entry[f"by_model_{model_key}_requests"] = entry.get(f"by_model_{model_key}_requests", 0) + requests
                entry[f"by_model_{model_key}_seconds"] = entry.get(f"by_model_{model_key}_seconds", 0.0) + duration_sec
                entry[f"by_model_{model_key}_cost_usd"] = entry.get(f"by_model_{model_key}_cost_usd", 0.0) + est_cost_usd
                entry["last_updated_utc"] = datetime.now(timezone.utc).isoformat()

        return {
            "model": model,
            "requests": requests,
            "input_chars": input_chars,
            "audio_seconds": round(duration_sec, 2),
            "estimated_audio_tokens": est_audio_tokens,
            "estimated_cost_usd": round(est_cost_usd, 6),
        }

    async def get_monthly_summary(self, month: str | None = None) -> dict[str, Any]:
        """Fetch usage summary for YYYY-MM (defaults to current UTC month)."""
        target_month = validate_month_key(month) if month else _current_month_key()
        raw: dict[str, Any] = {}

        if self._db is not None:
            snap = await self._db.collection(self._collection_name).document(target_month).get()
            if snap.exists:
                raw = snap.to_dict() or {}
        else:
            async with self._lock:
                raw = dict(self._mem.get(target_month, {}))

        total_sec = float(raw.get("audio_seconds", 0.0))
        est_cost = float(raw.get("estimated_cost_usd", 0.0))
        remaining = max(0.0, self.monthly_budget_usd - est_cost)

        return {
            "month": target_month,
            "requests": int(raw.get("requests", 0)),
            "input_chars": int(raw.get("input_chars", 0)),
            "total_audio_seconds": round(total_sec, 2),
            "total_audio_minutes": round(total_sec / 60.0, 2),
            "estimated_cost_usd": round(est_cost, 4),
            "monthly_credit_budget_usd": self.monthly_budget_usd,
            "estimated_remaining_usd": round(remaining, 4),
            "by_model": {
                "gemini-3.8-flash-lite-tts": {
                    "requests": int(raw.get("by_model_flash_lite_requests", 0)),
                    "audio_seconds": round(float(raw.get("by_model_flash_lite_seconds", 0.0)), 2),
                    "estimated_cost_usd": round(float(raw.get("by_model_flash_lite_cost_usd", 0.0)), 4),
                },
                "gemini-3.8-flash-tts": {
                    "requests": int(raw.get("by_model_flash_requests", 0)),
                    "audio_seconds": round(float(raw.get("by_model_flash_seconds", 0.0)), 2),
                    "estimated_cost_usd": round(float(raw.get("by_model_flash_cost_usd", 0.0)), 4),
                },
            },
            "note": (
                "Server-side real-time estimate in UTC based on 32 audio tokens/sec (conservative). "
                "Cloud Billing uses US Pacific month boundaries and has up to 24h reporting lag."
            ),
        }

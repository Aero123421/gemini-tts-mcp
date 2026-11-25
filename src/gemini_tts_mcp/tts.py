"""Gemini 3.8 TTS client wrapper with explicit HTTP timeout, concurrency semaphore, and retry backoff."""

from __future__ import annotations

import asyncio
import base64
import random
from dataclasses import dataclass
from typing import Any, Sequence

from google import genai


@dataclass(frozen=True)
class TurnSpec:
    text: str
    speaker: str = "narrator"
    voice: str = "Kore"
    style: str = ""


_NON_RETRYABLE_QUOTA_MARKERS = (
    "per day",
    "perday",
    "daily",
    "rpd",
    "quota_exhausted",
    "billing",
    "payment_required",
)
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class GeminiTTSClient:
    """Async-friendly wrapper around google-genai Interactions API for Gemini 3.8 TTS."""

    def __init__(
        self,
        api_key: str,
        max_concurrency: int = 4,
        timeout_sec: float = 60.0,
    ) -> None:
        self._timeout_sec = max(5.0, timeout_sec)
        self._client = genai.Client(
            api_key=api_key,
            http_options={
                "api_version": "v1beta",
                "timeout": int(self._timeout_sec * 1000),
            },
        )
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        msg = str(exc).lower()
        if any(m in msg for m in _NON_RETRYABLE_QUOTA_MARKERS):
            return False
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        if isinstance(code, int):
            return code in _RETRYABLE_STATUS_CODES
        return any(
            tok in msg
            for tok in ("429", "resource_exhausted", "500", "502", "503", "504", "timeout", "temporarily")
        )

    async def _call_with_retry(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Run blocking SDK call in a worker thread with semaphore, HTTP timeout & 429/5xx backoff."""
        max_attempts = 4
        base_delay = 1.5
        last_exc: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                async with self._semaphore:
                    return await asyncio.to_thread(fn, *args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if not self._is_retryable(exc) or attempt == max_attempts:
                    raise
                delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0.0, 0.8)
                await asyncio.sleep(delay)

        raise RuntimeError(f"TTS request failed after {max_attempts} attempts: {last_exc}")

    async def synthesize_single(
        self,
        text: str,
        model: str,
        voice: str = "Kore",
        style: str = "",
    ) -> bytes:
        """Generate a single-speaker WAV clip."""
        content_item: dict[str, Any] = {"type": "text", "text": text}
        if style.strip():
            content_item["annotations"] = [
                {"type": "speech_metadata", "style": style.strip()}
            ]

        interaction = await self._call_with_retry(
            self._client.interactions.create,
            model=model,
            input=[{"type": "user_input", "content": [content_item]}],
            response_format={"type": "audio"},
            generation_config={"speech_config": [{"voice": voice}]},
        )
        if not interaction.output_audio or not interaction.output_audio.data:
            raise RuntimeError("Gemini TTS returned no audio data.")
        return base64.b64decode(interaction.output_audio.data)

    async def synthesize_dialogue(
        self,
        turns: Sequence[TurnSpec],
        model: str,
    ) -> bytes:
        """Generate a multi-speaker dialogue WAV clip in one interaction."""
        if not turns:
            raise ValueError("Dialogue requires at least one turn.")

        speaker_voices: dict[str, str] = {}
        content_items: list[dict[str, Any]] = []
        for t in turns:
            speaker_voices.setdefault(t.speaker, t.voice or "Kore")
            meta: dict[str, Any] = {
                "type": "speech_metadata",
                "speaker": t.speaker,
            }
            if t.style.strip():
                meta["style"] = t.style.strip()
            content_items.append(
                {
                    "type": "text",
                    "text": t.text,
                    "annotations": [meta],
                }
            )

        if len(speaker_voices) == 1:
            only_voice = next(iter(speaker_voices.values()))
            speech_config: Any = [{"voice": only_voice}]
        else:
            speech_config = {
                "speakers": [
                    {"speaker": spk, "voice": v}
                    for spk, v in speaker_voices.items()
                ]
            }

        interaction = await self._call_with_retry(
            self._client.interactions.create,
            model=model,
            input=[{"type": "user_input", "content": content_items}],
            response_format={"type": "audio"},
            generation_config={"speech_config": speech_config},
        )
        if not interaction.output_audio or not interaction.output_audio.data:
            raise RuntimeError("Gemini TTS returned no audio data.")
        return base64.b64decode(interaction.output_audio.data)

    async def list_voices(
        self,
        query: str = "",
        gender: str = "",
        language_code: str = "",
        page_size: int = 30,
    ) -> list[dict[str, Any]]:
        """Query the Gemini Voices API (returns custom voices first, then prebuilt)."""
        kwargs: dict[str, Any] = {"page_size": min(100, max(1, page_size))}
        if query.strip():
            kwargs["search"] = query.strip()
        if gender.strip():
            kwargs["gender"] = [gender.strip().lower()]
        if language_code.strip():
            kwargs["language_code"] = [language_code.strip()]

        resp = await self._call_with_retry(self._client.voices.list, **kwargs)
        results: list[dict[str, Any]] = []
        for v in getattr(resp, "voices", None) or []:
            results.append(
                {
                    "id": getattr(v, "id", ""),
                    "name": getattr(v, "display_name", ""),
                    "language": getattr(v, "language_code", ""),
                    "gender": getattr(v, "gender", ""),
                    "pitch": getattr(v, "pitch", ""),
                    "description": getattr(v, "description", ""),
                }
            )
        return results

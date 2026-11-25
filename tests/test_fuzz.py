"""Deterministic fuzz & property-based tests (stdlib-only, zero external deps)."""

from __future__ import annotations

import io
import json
import random
import re
import struct
import unittest
import wave
import zipfile

from gemini_tts_mcp.audio import (
    SceneAudio,
    build_video_bundle,
    inspect_wav,
    sanitize_slug,
)
from gemini_tts_mcp.auth import (
    is_gemini_connected_app_client,
    sanitize_firestore_key_value,
)
from gemini_tts_mcp.rate_limit import SlidingWindowRateLimiter

_FIRESTORE_KEY_RE = re.compile(r"^K_[0-9a-f]{64}$")
_SLUG_VALID_RE = re.compile(r"^[a-z0-9_]+$")

_FUZZ_TOKENS = [
    "こんにちは。",
    "Gemini 3.8はすごい！",
    "「引用」と（括弧）",
    "ver 3.14.159",
    "<short pause>",
    "<laugh>",
    "<unclosed tag",
    "<<nested>>",
    "<script>alert(1)</script>",
    "../../../etc/passwd",
    "\\windows\\path",
    "\n\n\r\t",
    "   ",
    "🚀🔥🎧",
    "a" * 60,
    "、",
    "。",
    ".",
    "!",
    "?",
    "」",
    "』",
]


def _make_wav(sample_rate: int = 24000, duration_sec: float = 0.05) -> bytes:
    nframes = max(1, int(round(sample_rate * duration_sec)))
    vals = ([500, -500] * (nframes // 2 + 1))[:nframes]
    pcm = struct.pack(f"<{len(vals)}h", *vals)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


class TestFuzzInvariants(unittest.TestCase):
    def test_fuzz_video_bundle_srt_and_zip_safety(self) -> None:
        rng = random.Random(20261125)
        wav_pool = {
            dur: _make_wav(24000, dur)
            for dur in (0.002, 0.01, 0.05, 0.25, 0.75)
        }
        durations = list(wav_pool.keys())

        for case_idx in range(150):
            num_scenes = rng.randint(1, 5)
            scenes: list[SceneAudio] = []
            for i in range(1, num_scenes + 1):
                raw_id = "".join(rng.choices(_FUZZ_TOKENS, k=rng.randint(0, 4)))
                slug = sanitize_slug(raw_id, f"scene_{i:03d}")
                self.assertRegex(slug, _SLUG_VALID_RE)
                self.assertLessEqual(len(slug), 40)

                text = "".join(rng.choices(_FUZZ_TOKENS, k=rng.randint(1, 8)))
                dur = rng.choice(durations)
                wav_bytes = wav_pool[dur]
                info = inspect_wav(wav_bytes)
                scenes.append(
                    SceneAudio(
                        index=i,
                        slug=f"{i:03d}_{slug}",
                        filename=f"scenes/{i:03d}_{slug}.wav",
                        speaker="narrator",
                        voice="Kore",
                        style="",
                        text=text,
                        wav_bytes=wav_bytes,
                        sample_rate=info.sample_rate,
                        channels=info.channels,
                        bit_depth=info.sample_width * 8,
                        sample_frames=info.num_frames,
                        duration_sec=info.duration_sec,
                    )
                )

            raw_bundle = "".join(rng.choices(_FUZZ_TOKENS, k=rng.randint(0, 3)))
            gap_sec = rng.choice([0.0, 0.1, 0.4, 1.5])
            zip_bytes, combined_wav, srt_text, manifest = build_video_bundle(
                scenes=scenes,
                model="gemini-3.8-flash-lite-tts",
                gap_sec=gap_sec,
                bundle_name=raw_bundle,
            )

            # Invariant 1: Combined WAV is always valid with positive duration
            combined_info = inspect_wav(combined_wav)
            self.assertGreater(combined_info.num_frames, 0)

            # Invariant 2: Every SRT cue has strictly increasing non-zero timecodes
            prev_end: str | None = None
            for line in srt_text.splitlines():
                if " --> " in line:
                    start_tc, end_tc = line.split(" --> ")
                    self.assertLess(start_tc, end_tc, f"case {case_idx}: inverted SRT {line}")
                    if prev_end is not None:
                        self.assertLessEqual(prev_end, start_tc, f"case {case_idx}: overlapping SRT")
                    prev_end = end_tc

            # Invariant 3: ZIP contains no path traversal, absolute paths, or backslashes
            with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
                names = zf.namelist()
                self.assertEqual(len(names), len(set(names)))
                for name in names:
                    self.assertFalse(name.startswith("/"))
                    self.assertNotIn("..", name.split("/"))
                    self.assertNotIn("\\", name)
                manifest_names = [n for n in names if n.endswith("/manifest.json")]
                self.assertEqual(len(manifest_names), 1)
                parsed = json.loads(zf.read(manifest_names[0]).decode("utf-8"))
                self.assertEqual(len(parsed["scenes"]), num_scenes)
                self.assertEqual(parsed["total_duration_sec"], manifest["total_duration_sec"])

    def test_fuzz_firestore_key_sanitization_injective_and_valid(self) -> None:
        rng = random.Random(20261125)
        alphabet = "abcXYZ0123/_.-~@#% \t\n\x00日本語🚀"
        inputs: set[str] = {
            "",
            ".",
            "..",
            "__name__",
            "H_prefix",
            "S_prefix",
            "K_" + "0" * 64,
            "/" * 50,
            "x" * 3000,
        }
        while len(inputs) < 250:
            length = rng.choice([1, 5, 32, 200, 1600])
            inputs.add("".join(rng.choices(alphabet, k=length)))

        outputs = set()
        for raw in inputs:
            sanitized = sanitize_firestore_key_value(raw)
            self.assertRegex(sanitized, _FIRESTORE_KEY_RE)
            outputs.add(sanitized)
        self.assertEqual(len(outputs), len(inputs))

    def test_fuzz_gemini_redirect_uri_rejects_spoofing(self) -> None:
        spoofed_uris = [
            "",
            "not-a-url",
            "http://oauth-redirect.googleusercontent.com/r/app",
            "javascript://oauth-redirect.googleusercontent.com/r/app",
            "https://oauth-redirect.googleusercontent.com@evil.com/r/app",
            "https://evil.com/https://oauth-redirect.googleusercontent.com/r/app",
            "https://oauth-redirect.googleusercontent.com.evil.com/r/app",
            "https://evil-oauth-redirect.googleusercontent.com/r/app",
            "https://oauth-redirect.googleusercontent.com/",
            "https://oauth-redirect.googleusercontent.com/r",
            "https://oauth-redirect.googleusercontent.com/callback",
            "https://oauth-redirect.googleusercontent.com/R/uppercase-path",
            "https://chatgpt.com/connector_platform_oauth_redirect",
            "https://claude.ai/api/mcp/auth_callback",
        ]
        for uri in spoofed_uris:
            self.assertFalse(is_gemini_connected_app_client([uri]), f"Should reject: {uri!r}")
            self.assertFalse(
                is_gemini_connected_app_client(
                    ["https://oauth-redirect.googleusercontent.com/r/legit", uri]
                ),
                f"Mixed list should reject: {uri!r}",
            )

    def test_fuzz_rate_limiter_never_exceeds_hard_limits(self) -> None:
        rng = random.Random(20261125)
        now = [10_000.0]
        rl = SlidingWindowRateLimiter(
            warn_per_min=5,
            block_per_min=15,
            warn_per_hour=20,
            block_per_hour=40,
            clock=lambda: now[0],
        )
        users = ["u1@example.com", "u2@example.com", "u3@example.com"]
        for _ in range(300):
            now[0] += rng.choice([0.0, 0.5, 2.0, 15.0, 70.0])
            user = rng.choice(users)
            units = rng.choice([0, 1, 2, 5, 20])
            res = rl.check_and_record(user, units=units)
            self.assertLessEqual(res.count_last_min, rl.block_per_min)
            self.assertLessEqual(res.count_last_hour, rl.block_per_hour)
            if not res.allowed:
                self.assertTrue(bool(res.block_reason))


if __name__ == "__main__":
    unittest.main()

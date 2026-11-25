import os
import unittest
from datetime import date
from unittest.mock import patch

from gemini_tts_mcp.config import Settings, estimate_cost_usd
from gemini_tts_mcp.dialogue import validate_dialogue_voices

BASE_ENV = {
    "ALLOWED_EMAILS": "test@example.com",
    "BASE_URL": "https://example.com",
    "GOOGLE_OAUTH_CLIENT_ID": "test",
    "GOOGLE_OAUTH_CLIENT_SECRET": "test",
    "GEMINI_API_KEY": "test",
}


class TestConfiguration(unittest.TestCase):
    def test_invalid_guardrails(self):
        for key, value in (
            ("MAX_ACTIVE_JOBS", "0"),
            ("MAX_SEGMENTS", "51"),
            ("PORT", "invalid"),
            ("TTS_TIMEOUT_SEC", "nan"),
            ("JOB_TTL_SEC", "inf"),
            ("MONTHLY_BUDGET_USD", "-1"),
            ("RATE_WARN_PER_MIN", "31"),
            ("MAX_TOTAL_CHARS", "2"),
        ):
            with self.subTest(key=key), patch.dict(os.environ, {**BASE_ENV, key: value}, clear=True):
                with self.assertRaisesRegex(ValueError, key):
                    Settings.from_env()

    def test_explicit_rate_limit(self):
        with patch.dict(os.environ, {**BASE_ENV, "RATE_BLOCK_PER_MIN": "3"}, clear=True):
            self.assertEqual(Settings.from_env().rate_block_per_min, 3)

    def test_studio_input_rates(self):
        for year, expected in ((2026, 0.50), (2027, 1.00)):
            _, cost = estimate_cost_usd("gemini-3.8-flash-tts", 1_000_000, 0, date(year, 1, 1))
            self.assertEqual(cost, expected)

    def test_dialogue_limits(self):
        validate_dialogue_voices([("a", "voice_custom"), ("a", "voice_custom")])
        for turns in ([("a", "voice_custom"), ("b", "Kore")], [("a", "Kore"), ("b", "Puck"), ("c", "Kore")]):
            with self.assertRaisesRegex(ValueError, "output_mode='split'"):
                validate_dialogue_voices(turns)

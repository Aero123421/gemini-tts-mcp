"""Unit tests for two-stage sliding-window rate limiter."""

from __future__ import annotations

import unittest

from gemini_tts_mcp.rate_limit import SlidingWindowRateLimiter


class TestRateLimiter(unittest.TestCase):
    def test_prune_dormant_users(self):
        now = [0]
        limiter = SlidingWindowRateLimiter(clock=lambda: now[0])
        limiter.check_and_record("dormant")
        now[0] = 3601
        limiter.check_and_record("active")
        self.assertEqual(set(limiter._events), {"active"})

    def test_soft_warn_then_hard_block_per_minute(self) -> None:
        now = [1000.0]
        rl = SlidingWindowRateLimiter(
            warn_per_min=10,
            block_per_min=20,
            warn_per_hour=60,
            block_per_hour=120,
            clock=lambda: now[0],
        )

        for _ in range(9):
            res = rl.check_and_record("user@example.com")
            self.assertTrue(res.allowed)
            self.assertEqual(res.warnings, [])

        res10 = rl.check_and_record("user@example.com")
        self.assertTrue(res10.allowed)
        self.assertEqual(len(res10.warnings), 1)

        for _ in range(10):
            self.assertTrue(rl.check_and_record("user@example.com").allowed)

        res21 = rl.check_and_record("user@example.com")
        self.assertFalse(res21.allowed)
        self.assertIsNotNone(res21.block_reason)

        now[0] += 61.0
        res_after = rl.check_and_record("user@example.com")
        self.assertTrue(res_after.allowed)
        self.assertEqual(res_after.warnings, [])

    def test_oversized_single_batch_returns_clear_message(self) -> None:
        rl = SlidingWindowRateLimiter(warn_per_min=10, block_per_min=20)
        res = rl.check_and_record("user@example.com", units=25)
        self.assertFalse(res.allowed)
        self.assertIn("Split into smaller batches", res.block_reason or "")


if __name__ == "__main__":
    unittest.main()

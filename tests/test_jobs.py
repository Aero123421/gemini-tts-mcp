"""Unit tests for JobManager (LRU byte cap, TTL, cancellation) and UsageTracker (month validation)."""

from __future__ import annotations

import asyncio
import unittest

from gemini_tts_mcp.jobs import JobArtifacts, JobManager, JobState
from gemini_tts_mcp.usage import UsageTracker, validate_month_key


class TestJobsAndUsage(unittest.IsolatedAsyncioTestCase):
    async def test_fast_job_completes_within_sync_window(self) -> None:
        mgr = JobManager(max_cached_jobs=5)

        async def _runner(st: JobState) -> None:
            await asyncio.sleep(0.02)
            st.completed_scenes = 1
            st.result_summary = {"status": "completed", "job_id": st.job_id}

        job = mgr.create_job("a@b.com", "gemini-3.8-flash-lite-tts", "merged", 1, [], _runner)
        res = await mgr.wait_or_progress(job, timeout_sec=1.0)
        self.assertEqual(res["status"], "completed")

    async def test_slow_job_returns_working_then_long_polls_to_done(self) -> None:
        mgr = JobManager(max_cached_jobs=5)

        async def _runner(st: JobState) -> None:
            await asyncio.sleep(0.12)
            st.completed_scenes = 3
            st.result_summary = {"status": "completed", "job_id": st.job_id}

        job = mgr.create_job("a@b.com", "gemini-3.8-flash-lite-tts", "split", 3, [], _runner)
        first = await mgr.wait_or_progress(job, timeout_sec=0.03)
        self.assertEqual(first["status"], "working")

        second = await mgr.wait_or_progress(job, timeout_sec=1.0)
        self.assertEqual(second["status"], "completed")

    async def test_byte_cap_lru_eviction(self) -> None:
        # 2MB byte cap
        mgr = JobManager(max_cached_jobs=10, max_cache_bytes=2 * 1024 * 1024)

        async def _noop(st: JobState) -> None:
            mgr.set_artifacts(st, JobArtifacts(primary_wav=b"x" * (1200 * 1024), title="big"))
            st.result_summary = {"status": "completed", "job_id": st.job_id}

        j1 = mgr.create_job("a@b.com", "m", "merged", 1, [], _noop)
        await mgr.wait_or_progress(j1, timeout_sec=1.0)

        j2 = mgr.create_job("a@b.com", "m", "merged", 1, [], _noop)
        await mgr.wait_or_progress(j2, timeout_sec=1.0)

        # 2.4MB > 2MB cap -> j1 must be evicted, j2 retained
        self.assertIsNone(mgr.get(j1.job_id))
        self.assertIsNotNone(mgr.get(j2.job_id))

    async def test_cancelled_job_marks_failed_and_sets_event(self) -> None:
        mgr = JobManager()

        async def _forever(_st: JobState) -> None:
            await asyncio.sleep(10.0)

        j = mgr.create_job("a@b.com", "m", "merged", 1, [], _forever)
        await asyncio.sleep(0.01)
        await mgr.shutdown()
        self.assertEqual(j.status, "failed")
        self.assertTrue(j.done_event.is_set())

    async def test_usage_tracker_in_memory_and_month_validation(self) -> None:
        self.assertEqual(validate_month_key("2026-11"), "2026-11")
        for bad in ("2026-13", "../other", "2026/11", "2026-00", "invalid"):
            with self.assertRaises(ValueError):
                validate_month_key(bad)

        tracker = UsageTracker(monthly_budget_usd=100.0, gcp_project=None)
        await tracker.record("gemini-3.8-flash-lite-tts", input_chars=500, duration_sec=30.0, requests=2)
        summary = await tracker.get_monthly_summary()
        self.assertEqual(summary["requests"], 2)
        self.assertAlmostEqual(summary["total_audio_seconds"], 30.0, places=2)
        self.assertGreater(summary["estimated_cost_usd"], 0.0)


if __name__ == "__main__":
    unittest.main()

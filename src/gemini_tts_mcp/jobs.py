"""In-memory background job manager with byte-capped LRU, TTL, and long-polling."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


@dataclass
class JobArtifacts:
    """In-memory audio artifacts for a completed job (ZIP built on-demand to save RAM)."""

    primary_wav: bytes
    title: str
    bundle_name: str = "narration"
    srt_text: str | None = None
    manifest: dict[str, Any] | None = None
    scene_wavs: dict[str, bytes] = field(default_factory=dict)

    @property
    def byte_size(self) -> int:
        total = len(self.primary_wav)
        if self.srt_text:
            total += len(self.srt_text.encode("utf-8"))
        total += sum(len(b) for b in self.scene_wavs.values())
        return total


@dataclass
class JobState:
    job_id: str
    owner_email: str
    model: str
    output_mode: str
    total_scenes: int
    status: str = "working"  # working | completed | failed
    completed_scenes: int = 0
    created_at: float = field(default_factory=time.monotonic)
    completed_at: float | None = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    result_summary: dict[str, Any] | None = None
    artifacts: JobArtifacts | None = None
    done_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[Any] | None = None


class JobManager:
    """Manages bounded in-memory TTS jobs with byte-budget LRU and TTL eviction."""

    def __init__(
        self,
        max_cached_jobs: int = 20,
        max_cache_bytes: int = 256 * 1024 * 1024,
        ttl_sec: float = 900.0,
        clock: Callable[[], float] | None = None,
        max_active_jobs: int = 4,
    ) -> None:
        self.max_active_jobs = max_active_jobs
        self.max_cached_jobs = max(1, max_cached_jobs)
        self.max_cache_bytes = max_cache_bytes
        self.ttl_sec = ttl_sec
        self._clock = clock or time.monotonic
        self._jobs: OrderedDict[str, JobState] = OrderedDict()

    def _total_cached_bytes(self) -> int:
        return sum(
            st.artifacts.byte_size
            for st in self._jobs.values()
            if st.artifacts is not None
        )

    def _prune_expired(self) -> None:
        now = self._clock()
        expired = [
            jid
            for jid, st in self._jobs.items()
            if st.status != "working"
            and now - (st.completed_at if st.completed_at is not None else st.created_at) > self.ttl_sec
        ]
        for jid in expired:
            self._jobs.pop(jid, None)

    def _evict_if_needed(self, protect_job_id: str | None = None) -> None:
        self._prune_expired()
        # Evict oldest non-working jobs until both count and byte budgets are satisfied
        while (
            len(self._jobs) > self.max_cached_jobs
            or self._total_cached_bytes() > self.max_cache_bytes
        ):
            victim_id: str | None = None
            for jid, st in self._jobs.items():
                if jid == protect_job_id or st.status == "working":
                    continue
                victim_id = jid
                break
            if victim_id is None:
                break
            self._jobs.pop(victim_id, None)

    def set_artifacts(self, job: JobState, artifacts: JobArtifacts) -> None:
        """Attach completed artifacts and enforce byte-cap LRU eviction."""
        if artifacts.byte_size > self.max_cache_bytes:
            raise ValueError("Job artifacts exceed MAX_CACHE_BYTES.")
        job.artifacts = artifacts
        if job.job_id in self._jobs:
            self._jobs.move_to_end(job.job_id)
        self._evict_if_needed(protect_job_id=job.job_id)

    def create_job(
        self,
        owner_email: str,
        model: str,
        output_mode: str,
        total_scenes: int,
        warnings: list[str],
        runner: Callable[[JobState], Coroutine[Any, Any, None]],
    ) -> JobState:
        if sum(job.status == "working" for job in self._jobs.values()) >= self.max_active_jobs:
            raise ValueError("MAX_ACTIVE_JOBS reached. Wait for a running job to finish.")
        job_id = secrets.token_urlsafe(16)
        state = JobState(
            job_id=job_id,
            owner_email=owner_email,
            model=model,
            output_mode=output_mode,
            total_scenes=total_scenes,
            created_at=self._clock(),
            warnings=list(warnings),
        )
        self._jobs[job_id] = state
        self._evict_if_needed(protect_job_id=job_id)

        async def _wrapped() -> None:
            try:
                await runner(state)
                if state.status == "working":
                    state.status = "completed"
            except asyncio.CancelledError:
                state.status = "failed"
                state.error = "Job was cancelled before completion."
                raise
            except ValueError as exc:
                state.status = "failed"
                state.error = str(exc)
            except Exception as exc:  # noqa: BLE001
                logger.exception("TTS job %s failed", job_id[:8])
                state.status = "failed"
                # Return concise safe error without leaking internal stack or credentials
                state.error = f"Synthesis failed ({type(exc).__name__}). Check server logs for details."
            finally:
                state.completed_at = self._clock()
                state.done_event.set()
                self._evict_if_needed(protect_job_id=job_id)

        state.task = asyncio.create_task(_wrapped())
        return state

    def get(self, job_id: str) -> JobState | None:
        self._prune_expired()
        job = self._jobs.get(job_id)
        if job is not None:
            self._jobs.move_to_end(job_id)
        return job

    async def shutdown(self) -> None:
        """Cancel all running background tasks cleanly on server shutdown."""
        tasks = [
            st.task
            for st in self._jobs.values()
            if st.task is not None and not st.task.done()
        ]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def wait_or_progress(
        self,
        job: JobState,
        timeout_sec: float,
    ) -> dict[str, Any]:
        """Wait up to `timeout_sec` for completion; return final summary or progress."""
        if not job.done_event.is_set() and timeout_sec > 0:
            try:
                await asyncio.wait_for(job.done_event.wait(), timeout=timeout_sec)
            except asyncio.TimeoutError:
                pass

        if job.status == "completed" and job.result_summary is not None:
            return job.result_summary

        if job.status == "failed":
            return {
                "status": "failed",
                "job_id": job.job_id,
                "error": job.error or "Unknown synthesis error.",
                "warnings": job.warnings,
            }

        return {
            "status": "working",
            "job_id": job.job_id,
            "completed_scenes": job.completed_scenes,
            "total_scenes": job.total_scenes,
            "warnings": job.warnings,
            "next_action": f"Call get_job(job_id='{job.job_id}') to wait for completion.",
        }

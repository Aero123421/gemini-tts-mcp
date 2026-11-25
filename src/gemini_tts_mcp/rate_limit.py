"""Two-stage per-user sliding-window rate limiter (soft warn -> hard block)."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    warnings: list[str]
    block_reason: str | None
    count_last_min: int
    count_last_hour: int


class SlidingWindowRateLimiter:
    """In-memory per-user sliding-window limiter with minute and hour windows."""

    def __init__(
        self,
        warn_per_min: int = 10,
        block_per_min: int = 30,
        warn_per_hour: int = 60,
        block_per_hour: int = 120,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.warn_per_min = warn_per_min
        self.block_per_min = block_per_min
        self.warn_per_hour = warn_per_hour
        self.block_per_hour = block_per_hour
        self._clock = clock or time.monotonic
        self._next_cleanup = self._clock() + 60.0
        self._events: dict[str, deque[float]] = {}

    def check_and_record(self, user_id: str, units: int = 1) -> RateLimitResult:
        """Check limits for `user_id`. Records `units` generation events if allowed."""
        now = self._clock()
        if now >= self._next_cleanup:
            self._events = {user: q for user, q in self._events.items() if q and q[-1] > now - 3600}
            self._next_cleanup = now + 60.0
        units = max(1, units)

        if units > self.block_per_min:
            return RateLimitResult(
                allowed=False,
                warnings=[],
                block_reason=(
                    f"Single request size ({units} generations) exceeds the per-minute "
                    f"limit ({self.block_per_min}/min). Split into smaller batches."
                ),
                count_last_min=0,
                count_last_hour=0,
            )

        q = self._events.get(user_id)
        if q is not None:
            hour_cutoff = now - 3600.0
            while q and q[0] <= hour_cutoff:
                q.popleft()
            if not q:
                self._events.pop(user_id, None)
                q = None

        min_cutoff = now - 60.0
        count_hour = len(q) if q is not None else 0
        count_min = sum(1 for ts in q if ts > min_cutoff) if q is not None else 0

        projected_min = count_min + units
        projected_hour = count_hour + units

        if projected_min > self.block_per_min:
            return RateLimitResult(
                allowed=False,
                warnings=[],
                block_reason=(
                    f"Rate limit exceeded: {count_min}/{self.block_per_min} generations "
                    f"in the last minute (requested +{units}). "
                    "Do NOT retry automatically; inform the user and wait ~1 minute."
                ),
                count_last_min=count_min,
                count_last_hour=count_hour,
            )

        if projected_hour > self.block_per_hour:
            return RateLimitResult(
                allowed=False,
                warnings=[],
                block_reason=(
                    f"Hourly rate limit exceeded: {count_hour}/{self.block_per_hour} "
                    f"generations in the last hour (requested +{units}). "
                    "Do NOT retry automatically; inform the user."
                ),
                count_last_min=count_min,
                count_last_hour=count_hour,
            )

        if q is None:
            q = deque()
            self._events[user_id] = q
        for _ in range(units):
            q.append(now)

        warnings: list[str] = []
        if projected_min >= self.warn_per_min:
            warnings.append(
                f"High usage frequency: {projected_min} generations in the last minute "
                f"(hard stop at {self.block_per_min}/min). Confirm with the user before retrying repeatedly."
            )
        if projected_hour >= self.warn_per_hour:
            warnings.append(
                f"High hourly usage: {projected_hour} generations in the last hour "
                f"(hard stop at {self.block_per_hour}/hour). Confirm with the user if continuing."
            )

        return RateLimitResult(
            allowed=True,
            warnings=warnings,
            block_reason=None,
            count_last_min=projected_min,
            count_last_hour=projected_hour,
        )

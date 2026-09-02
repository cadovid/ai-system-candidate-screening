"""Small process-local request controls for the browser API.

These controls are a useful last line of defence for one application worker;
deployments should also enforce rate and body limits at the edge.  The
coordinator's idempotency and locks remain authoritative across workers.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    retry_after: int = 0


class SlidingWindowRateLimiter:
    """Bound requests per key in a rolling time window."""

    def __init__(self, max_requests: int = 60, window_seconds: float = 60.0) -> None:
        if max_requests < 1 or window_seconds <= 0:
            raise ValueError("rate-limit values must be positive")
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, *, now: float | None = None) -> RateLimitResult:
        current = time.monotonic() if now is None else now
        events = self._events[key]
        cutoff = current - self.window_seconds
        while events and events[0] <= cutoff:
            events.popleft()
        if len(events) >= self.max_requests:
            retry = max(1, int(events[0] + self.window_seconds - current + 0.999))
            return RateLimitResult(False, retry)
        events.append(current)
        # Avoid unbounded key growth when a worker is exposed to random IPs.
        if len(self._events) > 10_000:
            for stale_key, stale_events in list(self._events.items())[:1_000]:
                if not stale_events or stale_events[-1] <= cutoff:
                    self._events.pop(stale_key, None)
        return RateLimitResult(True)

    # Familiar alias for callers/tests.
    allow = check


class TurnConcurrencyLimit:
    """Non-blocking semaphore used around provider-bound turn requests."""

    def __init__(self, limit: int = 16) -> None:
        if limit < 1:
            raise ValueError("concurrency limit must be positive")
        self.limit = limit
        self._semaphore = asyncio.Semaphore(limit)

    async def acquire(self) -> bool:
        # The API should fail quickly under load; waiting here would tie up
        # request workers and make retries amplify pressure.
        if self._semaphore.locked():
            return False
        await self._semaphore.acquire()
        return True

    def release(self) -> None:
        self._semaphore.release()


__all__ = ["RateLimitResult", "SlidingWindowRateLimiter", "TurnConcurrencyLimit"]

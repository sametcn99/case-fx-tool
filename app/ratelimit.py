"""A small per-client ceiling on requests to the conversion endpoint.

The endpoint is unauthenticated and the rate cache holds a bounded number of
entries, so a caller who walks more distinct ``(from, to, date)`` keys than the
cache holds turns every request into an upstream call. Without a limit this
service is a free amplifier pointed at the ECB feed, and the throttling that
follows lands on *our* address, not the caller's.

This is deliberately in-process and approximate: a guard rail for one instance,
not a distributed quota. Something in front of the service doing the same job
properly is a reason to set ``FX_RATE_LIMIT_PER_MINUTE=0``, not a reason for
this to be absent by default.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable

Clock = Callable[[], float]

# The bucket table is itself attacker-influenced — one entry per client key —
# so it is bounded. Evicting the least recently seen client only resets that
# client to a full allowance, which degrades to "unlimited", never to "wrong".
MAX_TRACKED_CLIENTS = 4096


class RateLimiter:
    """A token bucket per client key, refilled at ``limit`` per window.

    A bucket rather than a fixed window so that ordinary bursty traffic — an
    agent asking three questions in a row — is not punished, while a sustained
    walk through cache keys is.
    """

    def __init__(
        self,
        limit: int,
        window_seconds: float = 60.0,
        clock: Clock | None = None,
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._clock = clock or time.monotonic
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()

    @property
    def enabled(self) -> bool:
        return self._limit > 0

    def allow(self, key: str) -> bool:
        """Spend one token for ``key``; return whether there was one to spend."""

        if not self.enabled:
            return True

        now = self._clock()
        tokens, updated_at = self._buckets.pop(key, (float(self._limit), now))
        tokens = min(
            float(self._limit),
            tokens + (now - updated_at) * self._limit / self._window,
        )

        allowed = tokens >= 1.0
        if allowed:
            tokens -= 1.0

        self._buckets[key] = (tokens, now)
        while len(self._buckets) > MAX_TRACKED_CLIENTS:
            self._buckets.popitem(last=False)
        return allowed

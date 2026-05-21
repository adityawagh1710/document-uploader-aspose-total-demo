"""Per-client token-bucket rate limiter.

In-memory rate limiting keyed by client IP. Token bucket lets short bursts
through while bounding sustained rate. State is per-process — multi-replica
deployments get N x the configured per-IP rate. Acceptable trade-off until
the service grows beyond a handful of replicas.

Memory is bounded via LRU eviction at `max_keys` entries.

Client identity precedence:
1. First IP in `X-Forwarded-For` (set by ALB/proxy), when `trust_xff` is True.
2. `request.client.host` fallback (direct connection).

NOTE: `X-Forwarded-For` is spoofable when the API is exposed directly. Deploy
behind a proxy that overwrites the header (ALB does), or set
`OFFICE_CONVERT_RATE_LIMIT_TRUST_XFF=0` if exposed directly.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass

from fastapi import Request


@dataclass
class RateLimitDecision:
    """Result of a rate-limit check. `allowed` says whether to serve."""

    allowed: bool
    limit: int  # tokens-per-minute configured ceiling
    remaining: int  # whole tokens left in the bucket post-decision
    reset_epoch_seconds: int  # epoch seconds when bucket will be full again
    retry_after_seconds: int  # seconds until at least one token is available (>=1 when denied)


class RateLimiter:
    """Token bucket per client identifier.

    Refill rate = `per_minute / 60` tokens/sec. Bucket capacity = `burst`.
    Each request costs 1 token. When the bucket is empty, the request is
    denied with a `retry_after` derived from the refill rate.
    """

    def __init__(self, *, per_minute: int, burst: int, max_keys: int) -> None:
        if per_minute < 1:
            raise ValueError("per_minute must be >= 1")
        if burst < 1:
            raise ValueError("burst must be >= 1")
        if max_keys < 1:
            raise ValueError("max_keys must be >= 1")
        self.per_minute = per_minute
        self.burst = burst
        self.max_keys = max_keys
        self._refill_per_sec = per_minute / 60.0
        # OrderedDict acts as an LRU: move_to_end on touch, popitem(last=False)
        # to evict the oldest. Stores (tokens, last_refill_time_monotonic).
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def check(self, client_id: str) -> RateLimitDecision:
        """Atomically refill the client's bucket and try to consume 1 token."""
        async with self._lock:
            now = time.monotonic()
            if client_id in self._buckets:
                tokens, last = self._buckets[client_id]
                # Refill since last seen.
                tokens = min(float(self.burst), tokens + (now - last) * self._refill_per_sec)
                self._buckets.move_to_end(client_id)
            else:
                tokens = float(self.burst)
                if len(self._buckets) >= self.max_keys:
                    self._buckets.popitem(last=False)

            allowed = tokens >= 1.0
            if allowed:
                tokens -= 1.0
            self._buckets[client_id] = (tokens, now)

            # Time to refill to full bucket from current tokens.
            seconds_to_full = (self.burst - tokens) / self._refill_per_sec
            reset_epoch = int(time.time() + seconds_to_full)

            if allowed:
                retry_after = 0
            else:
                # Always >= 1 — round up so caller gets a positive wait.
                seconds_to_one = (1.0 - tokens) / self._refill_per_sec
                retry_after = max(1, int(seconds_to_one) + 1)

            return RateLimitDecision(
                allowed=allowed,
                limit=self.per_minute,
                remaining=int(tokens),
                reset_epoch_seconds=reset_epoch,
                retry_after_seconds=retry_after,
            )


def client_id_for(request: Request, *, trust_xff: bool) -> str:
    """Resolve a client identifier for rate-limiting.

    When `trust_xff` is True, the first IP in `X-Forwarded-For` wins. This
    matches AWS ALB's behavior of appending the original client IP as the
    first entry. Falls back to `request.client.host`.
    """
    if trust_xff:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            first = xff.split(",", 1)[0].strip()
            if first:
                return first
    if request.client is not None:
        return request.client.host
    return "unknown"

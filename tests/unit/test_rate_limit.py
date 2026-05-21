"""Unit tests for the in-memory token-bucket rate limiter."""

from __future__ import annotations

from typing import Any

import pytest

from office_convert.rate_limit import RateLimiter, client_id_for


@pytest.mark.asyncio
async def test_first_request_allowed_with_full_bucket() -> None:
    rl = RateLimiter(per_minute=60, burst=3, max_keys=100)
    decision = await rl.check("1.2.3.4")
    assert decision.allowed
    assert decision.limit == 60
    # After consuming 1 of 3 tokens, 2 remain.
    assert decision.remaining == 2
    assert decision.retry_after_seconds == 0


@pytest.mark.asyncio
async def test_burst_then_deny() -> None:
    """Fire `burst` requests immediately — all succeed; next one denied."""
    rl = RateLimiter(per_minute=60, burst=3, max_keys=100)
    results = [await rl.check("1.2.3.4") for _ in range(3)]
    assert all(r.allowed for r in results)
    # Bucket should be empty enough that the 4th can't acquire a whole token.
    denied = await rl.check("1.2.3.4")
    assert not denied.allowed
    assert denied.retry_after_seconds >= 1


@pytest.mark.asyncio
async def test_separate_clients_have_independent_buckets() -> None:
    rl = RateLimiter(per_minute=60, burst=1, max_keys=100)
    a = await rl.check("1.1.1.1")
    b = await rl.check("2.2.2.2")
    assert a.allowed and b.allowed
    # Each client now exhausted independently.
    a2 = await rl.check("1.1.1.1")
    b2 = await rl.check("2.2.2.2")
    assert not a2.allowed and not b2.allowed


@pytest.mark.asyncio
async def test_lru_eviction_at_cap() -> None:
    """When max_keys is hit, oldest client is evicted."""
    rl = RateLimiter(per_minute=60, burst=1, max_keys=2)
    await rl.check("a")
    await rl.check("b")
    await rl.check("c")  # evicts "a"
    # "a" reappears with a fresh full bucket because its entry was dropped.
    a_again = await rl.check("a")
    assert a_again.allowed
    assert a_again.remaining == 0  # 1 - 1 = 0


@pytest.mark.asyncio
async def test_refill_over_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Advance monotonic clock: a denied client recovers after refill window."""
    fake_now = [0.0]

    def fake_monotonic() -> float:
        return fake_now[0]

    monkeypatch.setattr("office_convert.rate_limit.time.monotonic", fake_monotonic)

    rl = RateLimiter(per_minute=60, burst=1, max_keys=100)
    first = await rl.check("x")
    assert first.allowed
    denied = await rl.check("x")
    assert not denied.allowed

    # Refill rate is 60/60 = 1 token/sec. Advance 2 seconds.
    fake_now[0] += 2.0
    after = await rl.check("x")
    assert after.allowed


@pytest.mark.asyncio
async def test_invalid_init_args() -> None:
    with pytest.raises(ValueError):
        RateLimiter(per_minute=0, burst=1, max_keys=10)
    with pytest.raises(ValueError):
        RateLimiter(per_minute=10, burst=0, max_keys=10)
    with pytest.raises(ValueError):
        RateLimiter(per_minute=10, burst=1, max_keys=0)


# --- client_id_for ---


class _FakeRequest:
    def __init__(self, *, headers: dict[str, str], host: str | None) -> None:
        self.headers = headers
        self.client = type("C", (), {"host": host})() if host else None


def _req(*, xff: str | None = None, host: str | None = "10.0.0.1") -> Any:
    headers: dict[str, str] = {}
    if xff is not None:
        headers["x-forwarded-for"] = xff
    return _FakeRequest(headers=headers, host=host)


def test_client_id_prefers_first_xff_when_trusted() -> None:
    req = _req(xff="1.2.3.4, 5.6.7.8", host="10.0.0.1")
    assert client_id_for(req, trust_xff=True) == "1.2.3.4"


def test_client_id_falls_back_to_request_client_when_xff_missing() -> None:
    req = _req(xff=None, host="10.0.0.1")
    assert client_id_for(req, trust_xff=True) == "10.0.0.1"


def test_client_id_ignores_xff_when_not_trusted() -> None:
    req = _req(xff="1.2.3.4", host="10.0.0.1")
    assert client_id_for(req, trust_xff=False) == "10.0.0.1"


def test_client_id_handles_missing_client_object() -> None:
    req = _req(xff=None, host=None)
    assert client_id_for(req, trust_xff=True) == "unknown"

"""Resilience primitives: retry with jittered backoff, circuit breaker, timeouts, bulkhead.

Spec 10 (Integration Failure): BSS/OSS calls get a hard timeout budget (800 ms), a circuit breaker
and a graceful-degradation path - agents log the failure and defer to async reconciliation rather
than hang or hallucinate.
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Awaitable, Callable, Type, TypeVar

from convoiq.logging import get_logger
from convoiq.platform.metrics import REGISTRY

T = TypeVar("T")
log = get_logger("convoiq.resilience")
_cb_state = REGISTRY.gauge("convoiq_circuit_state", "0=closed 1=half-open 2=open", ["name"])
_retries = REGISTRY.counter("convoiq_retries_total", "retry attempts", ["name"])


class CircuitOpenError(RuntimeError):
    pass


class CircuitBreaker:
    def __init__(self, name: str, fail_threshold: int = 5, reset_s: float = 30.0, clock: Callable[[], float] = time.monotonic):
        self.name, self.fail_threshold, self.reset_s, self._clock = name, fail_threshold, reset_s, clock
        self._fails = 0
        self._opened_at: float | None = None
        self._half_open_inflight = False

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at >= self.reset_s:
            return "half-open"
        return "open"

    def _publish(self) -> None:
        _cb_state.set({"closed": 0, "half-open": 1, "open": 2}[self.state], name=self.name)

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        st = self.state
        if st == "open" or (st == "half-open" and self._half_open_inflight):
            self._publish()
            raise CircuitOpenError(f"circuit '{self.name}' is {st}")
        if st == "half-open":
            self._half_open_inflight = True
        try:
            res = await fn()
        except Exception:
            self._fails += 1
            if st == "half-open" or self._fails >= self.fail_threshold:
                self._opened_at = self._clock()
                log.warning("circuit opened", circuit=self.name, fails=self._fails)
            self._half_open_inflight = False
            self._publish()
            raise
        self._fails, self._opened_at, self._half_open_inflight = 0, None, False
        self._publish()
        return res


async def retry(fn: Callable[[], Awaitable[T]], *, attempts: int = 3, base: float = 0.2, cap: float = 5.0,
                retry_on: tuple[Type[BaseException], ...] = (Exception,), name: str = "op",
                sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> T:
    """Exponential backoff with full jitter."""
    last: BaseException | None = None
    for i in range(attempts):
        try:
            return await fn()
        except CircuitOpenError:
            raise
        except retry_on as e:  # type: ignore[misc]
            last = e
            if i == attempts - 1:
                break
            _retries.inc(name=name)
            await sleep(random.uniform(0, min(cap, base * 2 ** i)))
    assert last is not None
    raise last


async def with_timeout(coro: Awaitable[T], seconds: float, what: str = "operation") -> T:
    try:
        return await asyncio.wait_for(coro, seconds)
    except asyncio.TimeoutError as e:
        raise TimeoutError(f"{what} exceeded {seconds}s budget") from e


class Bulkhead:
    """Bound concurrency toward a dependency so one slow system cannot exhaust the worker."""
    def __init__(self, limit: int):
        self._sem = asyncio.Semaphore(limit)

    async def run(self, fn: Callable[[], Awaitable[T]]) -> T:
        async with self._sem:
            return await fn()


class TokenBucket:
    def __init__(self, rate_per_s: float, burst: int, clock: Callable[[], float] = time.monotonic):
        self.rate, self.burst, self._clock = rate_per_s, burst, clock
        self._tokens: dict[str, tuple[float, float]] = {}

    def allow(self, key: str, cost: float = 1.0) -> bool:
        now = self._clock()
        tokens, ts = self._tokens.get(key, (float(self.burst), now))
        tokens = min(self.burst, tokens + (now - ts) * self.rate)
        if tokens >= cost:
            self._tokens[key] = (tokens - cost, now)
            return True
        self._tokens[key] = (tokens, now)
        return False

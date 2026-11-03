"""Read-side enterprise tools (BSS / OSS / CRM) used by the agents - hardened for unreliable backends.

Every call: hard timeout budget (800 ms default, spec 10), retry w/ jitter, circuit breaker, bulkhead,
and a *typed* failure result so agents degrade gracefully instead of hallucinating missing data.
Evidence refs (``bss:line:L1002``) are returned with each result so claims can cite them.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from convoiq.config import Settings, get_settings
from convoiq.logging import get_logger
from convoiq.platform.resilience import Bulkhead, CircuitBreaker, CircuitOpenError, retry, with_timeout

log = get_logger("convoiq.tools")


@dataclass
class ToolResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    refs: list[str] = field(default_factory=list)
    latency_ms: float = 0.0

    @classmethod
    def fail(cls, err: str, ms: float = 0.0) -> "ToolResult":
        return cls(False, {}, err, [], ms)


class TelecomContext(Protocol):
    async def customer_profile(self, pseudo_id: str) -> ToolResult: ...
    async def bills(self, pseudo_id: str) -> ToolResult: ...
    async def tower_status(self, tower_id: str) -> ToolResult: ...


class InMemoryTelecomTools:
    """Dict-backed tools for unit tests / evals; supports seeding and failure injection."""

    def __init__(self):
        self.customers: dict[str, dict] = {}
        self.bills_: dict[str, list[dict]] = {}
        self.towers: dict[str, dict] = {}
        self.fail_with: str | None = None
        self.calls: list[str] = []

    def seed_customer(self, pid: str, **profile: Any) -> None:
        self.customers[pid] = profile

    async def _g(self, name: str, fn):
        self.calls.append(name)
        if self.fail_with:
            return ToolResult.fail(self.fail_with)
        return fn()

    async def customer_profile(self, pseudo_id):
        return await self._g("customer_profile", lambda: ToolResult(True, self.customers[pseudo_id], None, [f"bss:customer:{pseudo_id[:8]}"])
                             if pseudo_id in self.customers else ToolResult.fail("not_found"))

    async def bills(self, pseudo_id):
        return await self._g("bills", lambda: ToolResult(True, {"bills": self.bills_[pseudo_id]}, None,
                                                         [f"bss:bill:{b['bill_id']}" for b in self.bills_[pseudo_id]])
                             if pseudo_id in self.bills_ else ToolResult.fail("not_found"))

    async def tower_status(self, tower_id):
        return await self._g("tower_status", lambda: ToolResult(True, self.towers[tower_id], None, [f"oss:tower:{tower_id}"])
                             if tower_id in self.towers else ToolResult.fail("not_found"))


class HttpTelecomTools:
    def __init__(self, base_url: str, settings: Settings | None = None, token_provider=None, client: httpx.AsyncClient | None = None):
        s = settings or get_settings()
        self.base = base_url.rstrip("/")
        self.timeout = s.bss_timeout_s
        self.client = client or httpx.AsyncClient(timeout=self.timeout)
        self.token_provider = token_provider
        self.breaker = CircuitBreaker("telecom-context", s.circuit_fail_threshold, s.circuit_reset_s)
        self.bulkhead = Bulkhead(32)

    async def _get(self, path: str, ref_prefix: str) -> ToolResult:
        t0 = time.perf_counter()
        headers = {"authorization": f"Bearer {await self.token_provider()}"} if self.token_provider else {}

        async def once():
            r = await with_timeout(self.client.get(self.base + path, headers=headers), self.timeout, path)
            if r.status_code == 404:
                return ToolResult.fail("not_found")
            r.raise_for_status()
            return ToolResult(True, r.json(), None, [f"{ref_prefix}"])

        try:
            res = await self.bulkhead.run(lambda: self.breaker.call(lambda: retry(once, attempts=2, base=0.05, name="telecom")))
        except CircuitOpenError:
            res = ToolResult.fail("circuit_open")
        except Exception as e:
            res = ToolResult.fail(f"{type(e).__name__}")
        res.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        return res

    async def customer_profile(self, pseudo_id):
        return await self._get(f"/bss/customers/{pseudo_id}", f"bss:customer:{pseudo_id[:8]}")

    async def bills(self, pseudo_id):
        r = await self._get(f"/bss/customers/{pseudo_id}/bills", f"bss:bills:{pseudo_id[:8]}")
        if r.ok:
            r.refs = [f"bss:bill:{b['bill_id']}" for b in r.data.get("bills", [])]
        return r

    async def tower_status(self, tower_id):
        return await self._get(f"/oss/towers/{tower_id}", f"oss:tower:{tower_id}")

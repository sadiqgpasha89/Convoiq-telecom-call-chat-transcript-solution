"""Simulated BSS / OSS / CRM backend (stand-in for Amdocs/Ericsson, ServiceNow, Salesforce).

Purpose: (1) lets the whole system run and be tested end-to-end without enterprise access, (2) gives evals
authoritative ground-truth facts to verify agent claims against, (3) fault-injection (latency / errors) to
exercise circuit breakers, retries and deferred execution.  Idempotent on the ``Idempotency-Key``.
"""
from __future__ import annotations

import asyncio
import random
import uuid
from datetime import datetime, timezone
from typing import Any

from convoiq.platform.store import Store


class SimError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


class SimBackend:
    def __init__(self, store: Store):
        self.store = store
        self.latency_s = 0.0
        self.error_rate = 0.0
        self.rng = random.Random(0)

    # ---- chaos -----------------------------------------------------------------------------------
    def set_chaos(self, latency_ms: float = 0.0, error_rate: float = 0.0, seed: int | None = None) -> None:
        self.latency_s, self.error_rate = latency_ms / 1000.0, error_rate
        if seed is not None:
            self.rng = random.Random(seed)

    async def _chaos(self) -> None:
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        if self.error_rate and self.rng.random() < self.error_rate:
            raise SimError(503, "injected failure")

    # ---- seeding ---------------------------------------------------------------------------------
    async def seed_customer(self, pid: str, profile: dict[str, Any], bills: list[dict] | None = None) -> None:
        await self.store.put("sim_customer", pid, {"pseudo_id": pid, "authoritative": True, **profile})
        await self.store.put("sim_bills", pid, {"bills": bills or []})

    async def seed_tower(self, tower_id: str, status: str = "up", **extra: Any) -> None:
        await self.store.put("sim_tower", tower_id, {"tower_id": tower_id, "status": status, **extra})

    # ---- reads -----------------------------------------------------------------------------------
    async def customer(self, pid: str) -> dict:
        await self._chaos()
        d = await self.store.get("sim_customer", pid)
        if d is None:
            raise SimError(404, "customer not found")
        return d

    async def bills(self, pid: str) -> dict:
        await self._chaos()
        d = await self.store.get("sim_bills", pid)
        if d is None:
            raise SimError(404, "no bills")
        return d

    async def tower(self, tid: str) -> dict:
        await self._chaos()
        d = await self.store.get("sim_tower", tid)
        if d is None:
            raise SimError(404, "tower not found")
        return d

    # ---- writes (idempotent) ---------------------------------------------------------------------------
    async def _write(self, coll: str, idem_key: str | None, id_field: str, doc: dict) -> dict:
        await self._chaos()
        key = idem_key or uuid.uuid4().hex
        existing = await self.store.get(coll, key)
        if existing is not None:
            return {**existing, "replayed": True}
        out = {**doc, id_field: f"{coll}-{key[:12]}", "created_at": datetime.now(timezone.utc).isoformat()}
        await self.store.put(coll, key, out)
        return out

    async def credit(self, pid: str, body: dict, idem: str | None) -> dict:
        amt = float(body.get("amount_usd") or 0)
        if amt <= 0:
            raise SimError(422, "amount must be positive")
        await self.customer(pid)
        out = await self._write("sim_credit", idem, "credit_id", {"pseudo_id": pid, "amount_usd": amt, "reason": body.get("reason")})
        return out

    async def offer(self, pid: str, body: dict, idem: str | None) -> dict:
        return await self._write("sim_offer", idem, "offer_id", {"pseudo_id": pid, **body})

    async def ticket(self, body: dict, idem: str | None) -> dict:
        return await self._write("sim_ticket", idem, "ticket_id", body)

    async def note(self, iid: str, body: dict, idem: str | None) -> dict:
        return await self._write("sim_note", idem, "note_id", {"interaction_id": iid, **body})

    async def escalation(self, body: dict, idem: str | None) -> dict:
        return await self._write("sim_escalation", idem, "escalation_id", body)

    async def callback(self, body: dict, idem: str | None) -> dict:
        return await self._write("sim_callback", idem, "callback_id", body)

    async def list(self, coll: str, limit: int = 200) -> list[dict]:
        return await self.store.list(coll, limit=limit)

    async def credits_total(self, pid: str) -> float:
        return sum(float(c["amount_usd"]) for c in await self.store.list("sim_credit", limit=2000) if c.get("pseudo_id") == pid)

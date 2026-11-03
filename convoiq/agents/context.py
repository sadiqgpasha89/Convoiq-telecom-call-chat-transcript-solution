"""Context node: parallel, failure-tolerant lookups of BSS/OSS facts (spec edge case: API timeouts)."""
from __future__ import annotations

import asyncio
from typing import Any

from convoiq.agents.deps import AgentDeps
from convoiq.contracts import EnrichedInteraction


async def fetch_context(enr: EnrichedInteraction, deps: AgentDeps) -> dict[str, Any]:
    pid = enr.customer_pseudo_id
    tower = enr.meta.cell_tower_id or next(iter(enr.entities.get("towers", [])), None)
    jobs: dict[str, Any] = {}
    if pid:
        jobs["profile"] = deps.tools.customer_profile(pid)
        jobs["bills"] = deps.tools.bills(pid)
    if tower:
        jobs["tower"] = deps.tools.tower_status(tower)
    res = dict(zip(jobs, await asyncio.gather(*jobs.values(), return_exceptions=True)))
    ctx: dict[str, Any] = {"refs": [], "errors": [], "tower_id": tower, "authoritative": False}
    for k, r in res.items():
        if isinstance(r, Exception) or not getattr(r, "ok", False):
            ctx["errors"].append(f"{k}: {getattr(r, 'error', type(r).__name__)}")
            continue
        ctx[k] = r.data.get("bills") if k == "bills" else r.data
        ctx["refs"] += r.refs
    ctx["authoritative"] = bool((ctx.get("profile") or {}).get("authoritative", False))
    return ctx

"""Simulated enterprise backends (BSS/OSS/CRM) over HTTP - used by the orchestrator tools and action connectors.

NOT for production: exists so the full system can run and be tested end-to-end without access to a carrier's
systems, with seedable ground truth and fault injection.  Deployed to dev/staging only (Terraform variable).
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field

from convoiq.config import Settings, get_settings
from convoiq.platform.auth import Principal, require
from convoiq.platform.service import create_app
from convoiq.services.common import Service
from convoiq.telecom_sim.backend import SimBackend, SimError


class SeedCustomer(BaseModel):
    pseudo_id: str
    profile: dict = Field(default_factory=dict)
    bills: list[dict] = Field(default_factory=list)


class SeedTower(BaseModel):
    tower_id: str
    status: str = "up"


class Chaos(BaseModel):
    latency_ms: float = 0.0
    error_rate: float = 0.0
    seed: int | None = None


def build(settings: Settings | None = None, *, bus=None, store=None) -> Service:
    s = settings or get_settings()
    app = create_app("telecom-sim", "Simulated BSS / OSS / CRM backends (non-production)", s, bus=bus, store=store)
    sim = SimBackend(app.state.store)
    app.state.sim = sim

    def wrap(e: SimError):
        raise HTTPException(e.status, str(e))

    R = Depends(require("service", "ingest", "analyst"))

    @app.get("/bss/customers/{pid}")
    async def customer(pid: str, _=R):
        try:
            return await sim.customer(pid)
        except SimError as e:
            wrap(e)

    @app.get("/bss/customers/{pid}/bills")
    async def bills(pid: str, _=R):
        try:
            return await sim.bills(pid)
        except SimError as e:
            wrap(e)

    @app.get("/oss/towers/{tid}")
    async def tower(tid: str, _=R):
        try:
            return await sim.tower(tid)
        except SimError as e:
            wrap(e)

    def idem(request: Request) -> str | None:
        return request.headers.get("idempotency-key")

    @app.post("/bss/customers/{pid}/credits", status_code=201)
    async def credit(pid: str, body: dict, request: Request, _=Depends(require("service"))):
        try:
            return await sim.credit(pid, body, idem(request))
        except SimError as e:
            wrap(e)

    @app.post("/bss/customers/{pid}/offers", status_code=201)
    async def offer(pid: str, body: dict, request: Request, _=Depends(require("service"))):
        try:
            return await sim.offer(pid, body, idem(request))
        except SimError as e:
            wrap(e)

    @app.post("/oss/tickets", status_code=201)
    async def ticket(body: dict, request: Request, _=Depends(require("service"))):
        try:
            return await sim.ticket(body, idem(request))
        except SimError as e:
            wrap(e)

    @app.post("/crm/cases/{iid}/notes", status_code=201)
    async def note(iid: str, body: dict, request: Request, _=Depends(require("service"))):
        try:
            return await sim.note(iid, body, idem(request))
        except SimError as e:
            wrap(e)

    @app.post("/escalations", status_code=201)
    async def escalation(body: dict, request: Request, _=Depends(require("service"))):
        try:
            return await sim.escalation(body, idem(request))
        except SimError as e:
            wrap(e)

    @app.post("/callbacks", status_code=201)
    async def callback(body: dict, request: Request, _=Depends(require("service"))):
        try:
            return await sim.callback(body, idem(request))
        except SimError as e:
            wrap(e)

    # ---- admin: seeding + chaos + inspection -------------------------------------------------------------------------
    @app.post("/admin/seed/customer", status_code=204)
    async def seed_customer(b: SeedCustomer, _=Depends(require("admin"))):
        await sim.seed_customer(b.pseudo_id, b.profile, b.bills)

    @app.post("/admin/seed/tower", status_code=204)
    async def seed_tower(b: SeedTower, _=Depends(require("admin"))):
        await sim.seed_tower(b.tower_id, b.status)

    @app.post("/admin/chaos", status_code=204)
    async def chaos(c: Chaos, _=Depends(require("admin"))):
        sim.set_chaos(c.latency_ms, c.error_rate, c.seed)

    @app.get("/admin/records/{coll}")
    async def records(coll: str, _=Depends(require("admin", "analyst"))):
        if coll not in {"sim_credit", "sim_offer", "sim_ticket", "sim_note", "sim_escalation", "sim_callback"}:
            raise HTTPException(404, "unknown collection")
        return await sim.list(coll)

    return Service("telecom-sim", app, {}, state={"sim": sim})


def create():
    return build().app

"""Orchestrator service: multi-agent analysis of enriched interactions -> insights + *proposed* actions."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Depends
from pydantic import BaseModel

from convoiq.agents import AgentDeps, Orchestrator
from convoiq.agents.tools import HttpTelecomTools, TelecomContext
from convoiq.config import Settings, get_settings
from convoiq.contracts import (Analysis, EnrichedInteraction, EventEnvelope, TOPIC_ACTIONS_PROPOSED, TOPIC_ANALYZED, TOPIC_ENRICHED)
from convoiq.hitl import HitlQueue, review_reasons
from convoiq.logging import get_logger
from convoiq.models.bundle import ModelBundle
from convoiq.platform.auth import Principal, require
from convoiq.platform.bus import PermanentError
from convoiq.platform.service import add_push_consumer, create_app
from convoiq.services.common import Service, TokenProvider, make_event

log = get_logger("convoiq.orchestrator.svc")


class AnalyzeRequest(BaseModel):
    interaction: EnrichedInteraction
    publish: bool = False


def build(settings: Settings | None = None, *, bus=None, store=None, bundle: ModelBundle | None = None,
          tools: TelecomContext | None = None, llm=None) -> Service:
    s = settings or get_settings()
    app = create_app("orchestrator", "Multi-agent analysis and action planning", s, bus=bus, store=store)
    bundle = bundle or ModelBundle.load(s)
    if tools is None:
        tools = HttpTelecomTools(s.telecom_sim_url, s, token_provider=TokenProvider(s, s.telecom_sim_url))
    deps = AgentDeps.build(s, bundle, tools, llm)
    store_ = app.state.store

    async def budget_ok() -> bool:
        d = await store_.get("llm_budget", datetime.now(timezone.utc).strftime("%Y-%m-%d")) or {"usd": 0.0}
        return d["usd"] < s.llm_daily_budget_usd

    async def on_cost(c: float) -> None:       # soft cap (non-atomic); hard cap = Vertex quota + billing alert
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        d = await store_.get("llm_budget", day) or {"usd": 0.0}
        d["usd"] = d["usd"] + c
        await store_.put("llm_budget", day, d, ttl=3 * 86400)

    deps.budget_ok, deps.on_cost = budget_ok, on_cost
    orch = Orchestrator(deps)
    hitl = HitlQueue(store_)
    app.state.orchestrator, app.state.hitl = orch, hitl

    async def ready() -> bool:
        return bundle.issue is not None
    app.state.ready_checks["models"] = ready

    async def run(enr: EnrichedInteraction, trace_id: str | None = None, publish: bool = True) -> Analysis:
        a = await orch.analyze(enr)
        reasons = review_reasons(a, s.hitl_sample_rate)
        if reasons:
            await hitl.enqueue(a, reasons)
        if publish:
            bus_ = app.state.bus
            await bus_.publish(TOPIC_ANALYZED, make_event("interaction.analyzed", a.model_dump(mode="json"), "orchestrator",
                                                          ordering_key=a.customer_pseudo_id, trace_id=trace_id))
            for act in a.proposed_actions:
                await bus_.publish(TOPIC_ACTIONS_PROPOSED, make_event("action.proposed", act.model_dump(mode="json"), "orchestrator",
                                                                      ordering_key=a.customer_pseudo_id, trace_id=trace_id))
        return a

    async def on_enriched(ev: EventEnvelope) -> None:
        try:
            enr = EnrichedInteraction.model_validate(ev.payload)
        except Exception as e:
            raise PermanentError(f"invalid enriched interaction: {type(e).__name__}") from e
        await run(enr, ev.trace_id)

    handler = add_push_consumer(app, "/pubsub/enriched", on_enriched)

    @app.post("/v1/analyze", response_model=Analysis)
    async def analyze(req: AnalyzeRequest, _: Principal = Depends(require("ingest", "service", "analyst"))):
        return await run(req.interaction, publish=req.publish)

    @app.get("/v1/hitl/pending")
    async def pending(_: Principal = Depends(require("analyst", "approver"))):
        return await hitl.pending()

    class Verdict(BaseModel):
        root_cause_correct: bool | None = None
        compliance_correct: bool | None = None
        comment: str = ""

    @app.post("/v1/hitl/{review_id}")
    async def review(review_id: str, v: Verdict, p: Principal = Depends(require("analyst", "approver"))):
        doc = await hitl.resolve(review_id, p.subject, v.model_dump())
        if doc is None:
            from fastapi import HTTPException
            raise HTTPException(404, "review not found or already resolved")
        return doc

    @app.get("/v1/hitl/stats")
    async def stats(_: Principal = Depends(require("analyst", "approver"))):
        return await hitl.stats()

    return Service("orchestrator", app, {TOPIC_ENRICHED: handler}, state={"orch": orch, "run": run, "deps": deps})


def create():
    return build().app

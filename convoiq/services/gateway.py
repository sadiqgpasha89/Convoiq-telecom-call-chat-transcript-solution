"""API gateway: the single public entry point (Cloud Run behind HTTPS LB + Cloud Armor).

* ingestion (async, 202): raw transcripts -> raw topic; edge-redacted transcripts -> redacted topic after attestation verification
* sync ``/v1/analyze`` for demos / low-volume integrations: redaction -> enrichment -> orchestrator over service-to-service HTTP
* read-only passthroughs to analytics (insights) and the action broker (approvals) so clients have ONE base URL, one auth model.
"""
from __future__ import annotations

import httpx
from fastapi import Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from convoiq.config import Settings, get_settings
from convoiq.contracts import (Analysis, RawInteraction, RedactedInteraction, TOPIC_RAW, TOPIC_REDACTED)
from convoiq.logging import get_logger
from convoiq.platform.auth import Principal, require
from convoiq.platform.metrics import REGISTRY
from convoiq.platform.service import create_app
from convoiq.privacy import Redactor, Tokenizer, verify_attestation
from convoiq.services.common import Service, ServiceClient, make_event

log = get_logger("convoiq.gateway")
_ing = REGISTRY.counter("convoiq_gateway_ingested_total", "interactions accepted", ["path", "outcome"])
MAX_BATCH = 100
INSIGHT_PATHS = {"summary", "issues", "root-causes", "churn", "compliance", "agents", "towers", "incidents"}


class Batch(BaseModel):
    interactions: list[RawInteraction] = Field(min_length=1, max_length=MAX_BATCH)


class AnalyzeBody(BaseModel):
    interaction: RawInteraction


def build(settings: Settings | None = None, *, bus=None, store=None, clients: dict[str, httpx.AsyncClient] | None = None) -> Service:
    s = settings or get_settings()
    app = create_app("gateway", "ConvoIQ public API", s, bus=bus, store=store)
    clients = clients or {}
    svc = {
        "redaction": ServiceClient(s.redaction_url, s, clients.get("redaction")),
        "enrichment": ServiceClient(s.enrichment_url, s, clients.get("enrichment")),
        "orchestrator": ServiceClient(s.orchestrator_url, s, clients.get("orchestrator"), timeout=40.0),
        "analytics": ServiceClient(s.analytics_url, s, clients.get("analytics")),
        "broker": ServiceClient(s.action_broker_url, s, clients.get("broker")),
    }
    app.state.svc = svc
    tok = Tokenizer(s.attestation_key or s.pii_hmac_key or "local-dev-hmac-key-0123456789")
    scanner = Redactor(tok)
    st = app.state.store

    async def accept(raw: RawInteraction, key: str | None) -> str:
        first = await st.put_if_absent("ingest", f"{raw.tenant_id}:{key or raw.interaction_id}", {"id": raw.interaction_id}, ttl=7 * 86400)
        if not first:
            _ing.inc(path="raw", outcome="duplicate")
            return "duplicate"
        await app.state.bus.publish(TOPIC_RAW, make_event("interaction.raw", raw.model_dump(mode="json"), "gateway", ordering_key=raw.interaction_id))
        _ing.inc(path="raw", outcome="accepted")
        return "accepted"

    @app.post("/v1/interactions", status_code=202)
    async def ingest(raw: RawInteraction, request: Request, response: Response, p: Principal = Depends(require("ingest"))):
        if not s.cloud_redaction_enabled:
            raise HTTPException(403, "raw ingestion disabled: submit edge-redacted payloads to /v1/interactions/redacted")
        outcome = await accept(raw, request.headers.get("idempotency-key"))
        if outcome == "duplicate":
            response.status_code = 200
        return {"interaction_id": raw.interaction_id, "status": outcome}

    @app.post("/v1/interactions/batch", status_code=202)
    async def ingest_batch(b: Batch, p: Principal = Depends(require("ingest"))):
        if not s.cloud_redaction_enabled:
            raise HTTPException(403, "raw ingestion disabled")
        out = [{"interaction_id": r.interaction_id, "status": await accept(r, None)} for r in b.interactions]
        return {"results": out}

    @app.post("/v1/interactions/redacted", status_code=202)
    async def ingest_redacted(red: RedactedInteraction, p: Principal = Depends(require("ingest"))):
        problems = verify_attestation(red, tok, scanner, require_signature=s.require_attestation)
        if problems:
            _ing.inc(path="redacted", outcome="rejected")
            log.warning("redacted payload rejected", interaction_id=red.interaction_id, problems=problems)
            raise HTTPException(422, "attestation rejected: " + "; ".join(problems))
        first = await st.put_if_absent("ingest", f"{red.tenant_id}:{red.interaction_id}", {"id": red.interaction_id}, ttl=7 * 86400)
        if first:
            await app.state.bus.publish(TOPIC_REDACTED, make_event("interaction.redacted", red.model_dump(mode="json"), "gateway",
                                                                   ordering_key=red.customer_pseudo_id))
        _ing.inc(path="redacted", outcome="accepted" if first else "duplicate")
        return {"interaction_id": red.interaction_id, "status": "accepted" if first else "duplicate"}

    async def call(name: str, method: str, path: str, **kw) -> httpx.Response:
        try:
            r = await svc[name].request(method, path, **kw)
        except httpx.HTTPError as e:
            log.error("upstream unavailable", upstream=name, error=type(e).__name__)
            raise HTTPException(502, f"{name} unavailable") from e
        if r.status_code in (401, 403):      # our own service credential was refused: a deployment fault, never the caller's
            log.error("upstream rejected service credentials", upstream=name, status=r.status_code)
            raise HTTPException(502, f"{name} rejected gateway credentials")
        return r

    def passthrough(r: httpx.Response) -> Response:
        return Response(content=r.content, status_code=r.status_code, media_type=r.headers.get("content-type", "application/json"))

    @app.post("/v1/analyze", response_model=Analysis)
    async def analyze(body: AnalyzeBody, p: Principal = Depends(require("ingest", "analyst"))):
        r1 = await call("redaction", "POST", "/v1/redact", content=body.interaction.model_dump_json(), headers={"content-type": "application/json"})
        if r1.status_code != 200:
            raise HTTPException(r1.status_code if r1.status_code < 500 else 502, "redaction failed")
        r2 = await call("enrichment", "POST", "/v1/enrich", json={"interaction": r1.json(), "complete": True})
        if r2.status_code != 200:
            raise HTTPException(r2.status_code if r2.status_code < 500 else 502, "enrichment failed")
        r3 = await call("orchestrator", "POST", "/v1/analyze", json={"interaction": r2.json(), "publish": False})
        if r3.status_code != 200:
            raise HTTPException(502, "analysis failed")
        return JSONResponse(r3.json())

    @app.get("/v1/interactions/{iid}")
    async def get_interaction(iid: str, p: Principal = Depends(require("analyst", "ingest"))):
        return passthrough(await call("analytics", "GET", f"/v1/interactions/{iid}"))

    @app.get("/v1/insights/{name}")
    async def insights(name: str, request: Request, p: Principal = Depends(require("analyst"))):
        if name not in INSIGHT_PATHS:
            raise HTTPException(404, "unknown insight")
        return passthrough(await call("analytics", "GET", f"/v1/insights/{name}", params=dict(request.query_params)))

    @app.get("/v1/approvals")
    async def approvals(p: Principal = Depends(require("approver"))):
        return passthrough(await call("broker", "GET", "/v1/approvals"))

    @app.post("/v1/approvals/{action_id}")
    async def decide(action_id: str, request: Request, p: Principal = Depends(require("approver"))):
        body = await request.json()
        body["approver"] = p.subject                       # approver identity comes from the credential, never from the client
        return passthrough(await call("broker", "POST", f"/v1/approvals/{action_id}", json=body))

    return Service("gateway", app, {})


def create():
    return build().app

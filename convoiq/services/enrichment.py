"""Enrichment service: verifies the redaction attestation, then runs the classical-ML/NLP stack (CPU only, no LLM)."""
from __future__ import annotations

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from convoiq.config import Settings, get_settings
from convoiq.contracts import (EnrichedInteraction, EventEnvelope, RedactedInteraction, TOPIC_ENRICHED, TOPIC_REDACTED)
from convoiq.enrichment import Enricher
from convoiq.logging import get_logger
from convoiq.models.bundle import ModelBundle
from convoiq.platform.auth import Principal, require
from convoiq.platform.bus import PermanentError
from convoiq.platform.metrics import REGISTRY
from convoiq.platform.service import add_push_consumer, create_app
from convoiq.privacy import Redactor, Tokenizer, verify_attestation
from convoiq.services.common import Service, make_event

log = get_logger("convoiq.enrichment.svc")
_rej = REGISTRY.counter("convoiq_attestation_rejected_total", "redacted payloads rejected by cloud-side verification", [])


class EnrichRequest(BaseModel):
    interaction: RedactedInteraction
    complete: bool = True            # False = mid-call (live assist): uses the prefix-capable early resolution head


def build(settings: Settings | None = None, *, bus=None, store=None, bundle: ModelBundle | None = None) -> Service:
    s = settings or get_settings()
    app = create_app("enrichment", "NLP + ML enrichment of redacted interactions", s, bus=bus, store=store)
    bundle = bundle or ModelBundle.load(s)
    enricher = Enricher(bundle, s)
    # cloud-side canary scanner + (optional) signature verification
    tok = Tokenizer(s.attestation_key or s.pii_hmac_key or "local-dev-hmac-key-0123456789")
    scanner = Redactor(tok)
    app.state.bundle = bundle

    async def ready() -> bool:
        return bundle.issue is not None and bundle.rootcause is not None
    app.state.ready_checks["models"] = ready

    def check(red: RedactedInteraction) -> None:
        problems = verify_attestation(red, tok, scanner, require_signature=s.require_attestation)
        if problems:
            _rej.inc()
            raise PermanentError("attestation rejected: " + "; ".join(problems))

    async def on_redacted(ev: EventEnvelope) -> None:
        try:
            red = RedactedInteraction.model_validate(ev.payload)
        except Exception as e:
            raise PermanentError(f"invalid redacted interaction: {type(e).__name__}") from e
        check(red)
        enr = enricher.enrich(red)
        await app.state.bus.publish(TOPIC_ENRICHED, make_event("interaction.enriched", enr.model_dump(mode="json"), "enrichment",
                                                               ordering_key=enr.customer_pseudo_id, trace_id=ev.trace_id))

    handler = add_push_consumer(app, "/pubsub/redacted", on_redacted)

    @app.post("/v1/enrich", response_model=EnrichedInteraction)
    async def enrich(req: EnrichRequest, _: Principal = Depends(require("ingest", "service"))):
        try:
            check(req.interaction)
        except PermanentError as e:
            raise HTTPException(422, str(e)) from e
        return enricher.enrich(req.interaction, complete=req.complete)

    return Service("enrichment", app, {TOPIC_REDACTED: handler}, state={"enricher": enricher})


def create():
    return build().app

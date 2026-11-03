"""Redaction service - the privacy boundary.  Raw transcripts enter here and nothing downstream ever sees PII.

* consumes ``convoiq-interactions-raw`` (Pub/Sub push) -> publishes ``convoiq-interactions-redacted``
* dual-pass: if the residual scan still finds identifiers, a strict second pass runs; if findings persist the payload is
  *withheld* (quarantine collection + dead letter) rather than forwarded.
* subject erasure (GDPR Art.17) and audited re-identification endpoints (admin only).
Deployable in the sovereign/edge zone (OpenStack/GKE-on-prem) with identical code; only the bus differs.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from convoiq.config import Settings, get_settings
from convoiq.contracts import (EventEnvelope, RawInteraction, RedactedInteraction, TOPIC_RAW, TOPIC_REDACTED)
from convoiq.logging import get_logger
from convoiq.platform.auth import Principal, require
from convoiq.platform.bus import PermanentError
from convoiq.platform.metrics import REGISTRY
from convoiq.platform.service import add_push_consumer, create_app
from convoiq.privacy import Redactor, Tokenizer, make_vault
from convoiq.privacy.redactor import second_pass
from convoiq.services.common import Service, make_event

log = get_logger("convoiq.redaction")
_red = REGISTRY.counter("convoiq_redaction_total", "redacted interactions", ["outcome"])
_ent = REGISTRY.counter("convoiq_redaction_entities_total", "entities redacted", ["type"])


class EraseRequest(BaseModel):
    msisdn: str | None = None
    pseudo_id: str | None = None


class ReidRequest(BaseModel):
    token: str
    purpose: str


def redact_safely(redactor: Redactor, raw: RawInteraction) -> tuple[RedactedInteraction | None, str]:
    red = redactor.redact_interaction(raw)
    if red.attestation.residual_findings == 0:
        return red, "clean"
    red = second_pass(redactor, red)
    if red.attestation.residual_findings == 0:
        return red, "second_pass"
    return None, "withheld"


def build(settings: Settings | None = None, *, bus=None, store=None, redactor: Redactor | None = None) -> Service:
    s = settings or get_settings()
    app = create_app("redaction", "PII redaction, tokenisation and subject erasure", s, bus=bus, store=store)
    if redactor is None:
        key = s.pii_hmac_key or "local-dev-hmac-key-0123456789"
        redactor = Redactor(Tokenizer(key), make_vault(s))
    app.state.redactor = redactor

    async def publish(red: RedactedInteraction) -> None:
        for k, v in red.attestation.counts.items():
            _ent.inc(v, type=k)
        await app.state.bus.publish(TOPIC_REDACTED, make_event("interaction.redacted", red.model_dump(mode="json"), "redaction",
                                                               ordering_key=red.customer_pseudo_id))

    async def process(raw: RawInteraction) -> str:
        red, how = redact_safely(redactor, raw)
        _red.inc(outcome=how)
        if red is None:
            await app.state.store.put("redaction_quarantine", raw.interaction_id,
                                      {"interaction_id": raw.interaction_id, "tenant_id": raw.tenant_id, "reason": "residual PII after second pass"}, ttl=30 * 86400)
            raise PermanentError("residual PII after second pass; payload withheld")
        await publish(red)
        return how

    async def on_raw(ev: EventEnvelope) -> None:
        try:
            raw = RawInteraction.model_validate(ev.payload)
        except Exception as e:
            raise PermanentError(f"invalid raw interaction: {type(e).__name__}") from e
        await process(raw)

    handler = add_push_consumer(app, "/pubsub/raw", on_raw)

    @app.post("/v1/redact", response_model=RedactedInteraction)
    async def redact(raw: RawInteraction, _: Principal = Depends(require("ingest", "service"))):
        red, how = redact_safely(redactor, raw)
        _red.inc(outcome=how)
        if red is None:
            raise HTTPException(422, "payload could not be redacted safely")
        return red

    @app.post("/v1/erase")
    async def erase(req: EraseRequest, p: Principal = Depends(require("admin"))):
        pid = req.pseudo_id or (redactor.tok.pseudo_id(req.msisdn) if req.msisdn else None)
        if not pid:
            raise HTTPException(422, "msisdn or pseudo_id required")
        n = redactor.vault.erase_subject(pid) if redactor.vault else 0
        log.warning("subject erased", subject=pid[:8], tokens=n, by=p.subject)
        return {"pseudo_id": pid, "tokens_erased": n}

    @app.post("/v1/reidentify")
    async def reidentify(req: ReidRequest, p: Principal = Depends(require("admin"))):
        if redactor.vault is None:
            raise HTTPException(501, "no vault configured")
        val = redactor.vault.get(req.token, p.subject, req.purpose)
        if val is None:
            raise HTTPException(404, "token not found (erased or unknown)")
        return {"value": val}

    return Service("redaction", app, {TOPIC_RAW: handler}, state={"process": process})


def create():
    return build().app

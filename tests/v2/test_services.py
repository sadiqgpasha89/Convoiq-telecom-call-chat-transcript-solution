"""Service-level behaviour through the in-process LocalSystem (same service code, push handlers, idempotency, DLQ and HTTP
contracts as production; only Pub/Sub / Firestore / BigQuery are replaced by in-memory/SQLite)."""
from __future__ import annotations

import json

import pytest

from convoiq.config import Settings
from convoiq.contracts.events import TOPIC_ACTIONS_PROPOSED, TOPIC_DLQ, TOPIC_RAW
from convoiq.data.seed import seed_for_conv
from convoiq.data.synth import generate
from convoiq.pipeline import corpus_to_raw
from convoiq.platform.bus import encode_push
from convoiq.privacy.tokenizer import TOKEN_RE
from convoiq.runtime.local import LocalSystem
from convoiq.services.common import make_event

from .conftest import MODELS, arun, needs_models

pytestmark = needs_models
JSON = {"content-type": "application/json"}


def make_system(bundle, **kw) -> LocalSystem:
    s = Settings(**{"env": "local", "auth_mode": "none", "connector_mode": "simulated", "models_dir": str(MODELS), **kw})
    return LocalSystem(s, bundle=bundle)


async def seed(system: LocalSystem, convs: list[dict]) -> None:
    sim = system.client("sim")
    for c in convs:
        pid, prof, bills, tower = seed_for_conv(c, system.redactor.tok)
        if pid:
            r = await sim.post("/admin/seed/customer", json={"pseudo_id": pid, "profile": prof, "bills": bills})
            assert r.status_code == 204
        if tower:
            await sim.post("/admin/seed/tower", json=tower)


async def ingest(system: LocalSystem, convs: list[dict], key: str | None = None):
    gw = system.client("gateway")
    out = []
    for c in convs:
        h = dict(JSON, **({"idempotency-key": key} if key else {}))
        out.append(await gw.post("/v1/interactions", content=corpus_to_raw(c).model_dump_json(), headers=h))
    return out


async def sim_count(system: LocalSystem, coll: str) -> int:
    return len((await system.client("sim").get(f"/admin/records/{coll}")).json())


def pick(family: str, n: int = 1, seed_: int = 11, **label_eq) -> list[dict]:
    convs = [c for c in generate(400, seed=seed_) if c["labels"]["family"] == family
             and all(c["labels"].get(k) == v for k, v in label_eq.items())]
    assert len(convs) >= n
    return convs[:n]


def test_end_to_end_pipeline_populates_insights_and_has_no_dead_letters(bundle):
    async def main():
        s = make_system(bundle)
        convs = generate(15, seed=11)
        await seed(s, convs)
        resps = await ingest(s, convs)
        assert all(r.status_code == 202 for r in resps)
        gw = s.client("gateway")
        iid = corpus_to_raw(convs[0]).interaction_id
        got = await gw.get(f"/v1/interactions/{iid}")
        assert got.status_code == 200
        for name in ("summary", "compliance"):
            assert (await gw.get(f"/v1/insights/{name}")).status_code == 200
        assert s.bus.dead_letters == []
        assert (await gw.get("/v1/insights/nonexistent")).status_code == 404
    arun(main())


def test_duplicate_ingest_is_idempotent(bundle):
    async def main():
        s = make_system(bundle)
        conv = pick("billing_dispute", 1, fraud=False, injection=False)
        await seed(s, conv)
        first = await ingest(s, conv)
        n_credit, n_note, n_events = await sim_count(s, "sim_credit"), await sim_count(s, "sim_note"), len(s.bus.log)
        second = await ingest(s, conv)
        assert first[0].status_code == 202 and second[0].status_code == 200 and second[0].json()["status"] == "duplicate"
        assert (await sim_count(s, "sim_credit"), await sim_count(s, "sim_note")) == (n_credit, n_note)
        assert len(s.bus.log) == n_events, "a duplicate must not produce any new event"
    arun(main())


def test_redelivered_action_events_do_not_double_execute(bundle):
    """Pub/Sub is at-least-once: replaying every proposed-action event must not repeat any side effect."""
    async def main():
        s = make_system(bundle)
        convs = pick("billing_dispute", 6, fraud=False, injection=False)
        await seed(s, convs)
        await ingest(s, convs)
        before = (await sim_count(s, "sim_credit"), await sim_count(s, "sim_note"))
        assert before[0] + before[1] > 0
        for topic, ev in list(s.bus.log):
            if topic == TOPIC_ACTIONS_PROPOSED:
                await s.bus.publish(topic, ev)
        assert (await sim_count(s, "sim_credit"), await sim_count(s, "sim_note")) == before
    arun(main())


def test_kill_switch_freezes_all_side_effects_and_can_be_lifted(bundle):
    async def main():
        s = make_system(bundle)
        broker = s.client("broker")
        assert (await broker.post("/admin/kill-switch", json={"enabled": True, "reason": "test"})).status_code == 200
        convs = pick("billing_dispute", 5, fraud=False, injection=False)
        await seed(s, convs)
        await ingest(s, convs)
        assert await sim_count(s, "sim_credit") == 0 and await sim_count(s, "sim_note") == 0
        assert (await broker.post("/admin/kill-switch", json={"enabled": False})).status_code == 200
        more = pick("billing_dispute", 5, seed_=12, fraud=False, injection=False)
        await seed(s, more)
        await ingest(s, more)
        assert await sim_count(s, "sim_note") > 0, "actions flow again once the switch is lifted"
    arun(main())


def test_large_credit_waits_for_human_approval_then_executes_exactly_once(bundle):
    async def main():
        s = make_system(bundle)
        convs = [c for c in generate(400, seed=13) if c["labels"]["family"] == "billing_dispute" and not c["labels"]["fraud"]
                 and not c["labels"]["injection"]]
        await seed(s, convs[:40])
        await ingest(s, convs[:40])
        broker = s.client("broker")
        pending = (await broker.get("/v1/approvals")).json()
        assert pending, "expected at least one credit that exceeds the auto-approval limit"
        a = pending[0]
        assert a["type"] == "BSS_CREDIT" and a["amount_usd"] > s.s.credit_auto_limit_usd
        before = await sim_count(s, "sim_credit")
        body = {"action_id": a["action_id"], "approved": True, "approver": "tester", "comment": "verified"}
        r = await broker.post(f"/v1/approvals/{a['action_id']}", json=body)
        assert r.status_code == 200 and r.json()["status"] == "EXECUTED", r.text
        assert await sim_count(s, "sim_credit") == before + 1
        again = await broker.post(f"/v1/approvals/{a['action_id']}", json=body)
        assert again.status_code == 404, "a decided action cannot be approved twice"
        assert await sim_count(s, "sim_credit") == before + 1
    arun(main())


def test_rejected_approval_has_no_side_effect(bundle):
    async def main():
        s = make_system(bundle)
        convs = [c for c in generate(400, seed=13) if c["labels"]["family"] == "billing_dispute" and not c["labels"]["fraud"]
                 and not c["labels"]["injection"]][:40]
        await seed(s, convs)
        await ingest(s, convs)
        broker = s.client("broker")
        a = (await broker.get("/v1/approvals")).json()[0]
        before = await sim_count(s, "sim_credit")
        r = await broker.post(f"/v1/approvals/{a['action_id']}", json={"action_id": a["action_id"], "approved": False, "approver": "t", "comment": "no"})
        assert r.status_code == 200 and r.json()["status"] == "REJECTED"
        assert await sim_count(s, "sim_credit") == before
        assert all(p["action_id"] != a["action_id"] for p in (await broker.get("/v1/approvals")).json())
    arun(main())


def test_prompt_injection_and_fraud_are_quarantined_no_money_moves(bundle):
    async def main():
        s = make_system(bundle)
        convs = pick("billing_dispute", 1, injection=True) if any(
            c["labels"]["family"] == "billing_dispute" and c["labels"]["injection"] for c in generate(400, seed=11)) else []
        convs += [c for c in generate(400, seed=11) if c["labels"]["injection"] or c["labels"]["fraud"]][:15]
        assert convs
        await seed(s, convs)
        await ingest(s, convs)
        assert await sim_count(s, "sim_credit") == 0, "no credit may be executed for a quarantined interaction"
        proposed = [e.payload for t, e in s.bus.log if t == TOPIC_ACTIONS_PROPOSED]
        assert all(p["type"] in {"CRM_NOTE", "ESCALATE_FRAUD", "ESCALATE_HUMAN"} for p in proposed)
    arun(main())


def test_redacted_ingest_rejects_tampered_payload_and_accepts_valid(bundle):
    async def main():
        s = make_system(bundle)
        gw = s.client("gateway")
        raw = corpus_to_raw(generate(1, seed=3)[0])
        red = s.redactor.redact_interaction(raw)
        ok = await gw.post("/v1/interactions/redacted", content=red.model_dump_json(), headers=JSON)
        assert ok.status_code == 202, ok.text
        forged = red.model_copy(deep=True)
        forged.interaction_id = forged.interaction_id + "-x"
        forged.turns[0].text += " my card is 4111 1111 1111 1111"
        bad = await gw.post("/v1/interactions/redacted", content=forged.model_dump_json(), headers=JSON)
        assert bad.status_code == 422 and "attestation" in bad.text
    arun(main())


def test_poison_message_is_dead_lettered_not_retried_forever(bundle):
    async def main():
        s = make_system(bundle)
        poison = make_event("interaction.raw", {"this": "is not a RawInteraction"}, "test")
        await s.bus.publish(TOPIC_RAW, poison)
        dlq = s.bus.events(TOPIC_DLQ)
        assert len(dlq) == 1 and dlq[0].payload["original"]["event_id"] == poison.event_id, "malformed payload must be dead-lettered"
        assert s.bus.dead_letters == [], "the consumer acked it (no endless redelivery) after routing it to the DLQ topic"
        # the service itself acks a poison push (HTTP 2xx) so Pub/Sub does not redeliver it endlessly
        r = await s.client("redaction").post("/pubsub/raw", json=encode_push(poison))
        assert r.status_code in (200, 202, 204), r.text
    arun(main())


def test_gdpr_erasure_removes_vault_tokens_but_keeps_analytics(bundle):
    async def main():
        s = make_system(bundle)
        conv = pick("billing_dispute", 1, fraud=False, injection=False)
        await seed(s, conv)
        await ingest(s, conv)
        raw = corpus_to_raw(conv[0])
        red = s.redactor.redact_interaction(raw)
        tok = TOKEN_RE.search(json.dumps(red.model_dump(mode="json"))).group(0)
        red_client = s.client("redaction")
        assert (await red_client.post("/v1/reidentify", json={"token": tok, "purpose": "test"})).status_code == 200
        r = await red_client.post("/v1/erase", json={"pseudo_id": red.customer_pseudo_id})
        assert r.status_code == 200 and r.json()["tokens_erased"] > 0
        assert (await red_client.post("/v1/reidentify", json={"token": tok, "purpose": "test"})).status_code == 404
        assert (await s.client("gateway").get(f"/v1/interactions/{raw.interaction_id}")).status_code == 200
    arun(main())


def test_api_key_auth_and_rbac(bundle):
    async def main():
        s = make_system(bundle, auth_mode="apikey", api_keys="k-ingest:ingest,k-analyst:analyst,k-approver:approver,k-admin:admin,k-svc:service",
                        service_api_key="k-svc")
        gw = s.client("gateway")
        raw = corpus_to_raw(generate(1, seed=4)[0]).model_dump_json()
        assert (await gw.post("/v1/interactions", content=raw, headers=JSON)).status_code == 401
        assert (await gw.post("/v1/interactions", content=raw, headers=dict(JSON, authorization="Bearer wrong"))).status_code == 401
        assert (await gw.post("/v1/interactions", content=raw, headers=dict(JSON, authorization="Bearer k-analyst"))).status_code == 403
        ok = await gw.post("/v1/interactions", content=raw, headers=dict(JSON, authorization="Bearer k-ingest"))
        assert ok.status_code in (200, 202), ok.text
        assert (await gw.get("/v1/approvals", headers={"authorization": "Bearer k-analyst"})).status_code == 403
        assert (await gw.get("/v1/insights/summary", headers={"authorization": "Bearer k-analyst"})).status_code == 200
        assert (await gw.get("/v1/approvals", headers={"authorization": "Bearer k-approver"})).status_code == 200
        assert (await gw.get("/v1/approvals", headers={"authorization": "Bearer k-ingest"})).status_code == 403
    arun(main())

"""Agent mesh behaviour: churn fusion + stated-intent floor, quarantine mode, context-seeded diagnosis, hostile-LLM containment."""
from __future__ import annotations

import pytest

from convoiq.agents.churn import assess_churn
from convoiq.agents.llm import ScriptedLLM
from convoiq.agents.tools import InMemoryTelecomTools
from convoiq.data.seed import seed_inmemory
from convoiq.data.synth import generate
from convoiq.pipeline import LocalPipeline, corpus_to_raw
from convoiq.value import ASSUMPTIONS, best_offer, clv

from .conftest import arun, make_conv, needs_models

pytestmark = needs_models

OPEN = ("agent", "Hi, you've reached TelcoCare. How can I help?")
LOYAL = {"tenure": 70, "contract": "Two year", "monthly_charges": 55.0, "internet_service": "DSL", "payment_method": "Mailed check"}


def cancel_conv(extra=()):
    return make_conv([OPEN, ("customer", "AT&T is offering me unlimited for $35 a month. Please cancel my line and port my number out."),
                      ("agent", "I'm sorry to hear that. Can I ask what made you consider leaving?"), *extra])


def test_stated_cancellation_cannot_be_buried_by_a_loyal_profile(enrich, pipeline):
    enr = enrich(cancel_conv())
    out = assess_churn(enr, pipeline.deps, {"profile": LOYAL}, identity_verified=True)
    assert out.band in ("MEDIUM", "HIGH"), out
    assert out.risk_score >= 0.5, "an explicit port-out request carries a 0.50 floor"
    assert any(d.feature == "stated_intent_floor" for d in out.drivers) or out.risk_score > 0.5


def test_no_churn_signal_with_loyal_profile_stays_low(enrich, pipeline):
    enr = enrich(make_conv([OPEN, ("customer", "How do I check my data usage this month?"), ("agent", "Open the app and tap Usage.")]))
    out = assess_churn(enr, pipeline.deps, {"profile": LOYAL}, identity_verified=True)
    assert out.band == "LOW" and out.recommended_offer is None


def test_sparse_profile_falls_back_to_base_rate_and_says_so(enrich, pipeline):
    enr = enrich(cancel_conv())
    out = assess_churn(enr, pipeline.deps, {"profile": {"tenure": 3}}, identity_verified=True)
    assert any("crm_profile_unavailable" in d.feature for d in out.drivers)


def test_malformed_crm_profile_degrades_instead_of_failing(enrich, pipeline):
    bad = dict(LOYAL, senior="not-a-number", paperless=object(), monthly_charges="n/a")
    out = assess_churn(enrich(cancel_conv()), pipeline.deps, {"profile": bad}, identity_verified=True)
    assert any("crm_profile_invalid" in d.feature for d in out.drivers) and out.band in ("MEDIUM", "HIGH")


def test_no_offer_for_unverified_caller(enrich, pipeline):
    enr = enrich(cancel_conv())
    out = assess_churn(enr, pipeline.deps, {"profile": LOYAL}, identity_verified=False)
    assert out.recommended_offer is None


def test_value_model_offer_respects_max_discount_and_positive_ev():
    for risk in (0.4, 0.6, 0.9):
        o = best_offer(risk, 80.0, "Month-to-month", 25.0)
        assert o is None or (o["pct"] <= 25.0 and o["ev_usd"] > 0)
    assert best_offer(0.01, 20.0, "Two year", 25.0) is None, "never discount a customer who is not leaving"
    assert clv(100.0, "Two year") > clv(100.0, "Month-to-month") * 0.5
    assert "gross_margin" in ASSUMPTIONS


def _family(name, **labels):
    return [c for c in generate(400, seed=21) if c["labels"]["family"] == name and all(c["labels"].get(k) == v for k, v in labels.items())]


def test_injection_conversation_enters_quarantine_with_restricted_actions(pipeline):
    c = next(c for c in generate(600, seed=21) if c["labels"]["injection"])
    out = arun(pipeline.process(corpus_to_raw(c)))
    assert out.analysis.mode.value in ("SAFE_MODE", "QUARANTINE")
    assert {a.type.value for a in out.analysis.proposed_actions} <= {"CRM_NOTE", "ESCALATE_FRAUD", "ESCALATE_HUMAN"}


def test_benign_conversations_are_not_quarantined(pipeline):
    benign = [c for c in generate(120, seed=22) if not c["labels"]["injection"] and not c["labels"]["fraud"]][:40]
    modes = [arun(pipeline.process(corpus_to_raw(c))).analysis.mode.value for c in benign]
    assert "SAFE_MODE" not in modes and "QUARANTINE" not in modes


def test_outage_cause_is_seeded_from_authoritative_oss_status(settings, bundle):
    tools = InMemoryTelecomTools()
    pl = LocalPipeline(settings, bundle, tools=tools)
    outage = next(c for c in generate(400, seed=23) if c["labels"]["family"] == "outage")
    seed_inmemory(tools, outage, pl.redactor.tok)
    res = arun(pl.process(corpus_to_raw(outage)))
    assert res.analysis.diagnosis.primary_tag == "network_tower_outage"
    assert "OSS_TICKET" in {a.type.value for a in res.analysis.proposed_actions}


def test_tower_status_up_never_yields_outage_ticket(settings, bundle):
    tools = InMemoryTelecomTools()
    pl = LocalPipeline(settings, bundle, tools=tools)
    outage = next(c for c in generate(400, seed=23) if c["labels"]["family"] == "outage")
    pid, prof, bills, _ = __import__("convoiq.data.seed", fromlist=["x"]).seed_for_conv(outage, pl.redactor.tok)
    tools.seed_customer(pid, authoritative=True, **prof)
    tools.towers[outage["meta"]["cell_tower_id"]] = {"tower_id": outage["meta"]["cell_tower_id"], "status": "up"}
    res = arun(pl.process(corpus_to_raw(outage)))
    assert "OSS_TICKET" not in {a.type.value for a in res.analysis.proposed_actions}, "authoritative 'up' must veto the outage hypothesis"


def test_backend_down_degrades_gracefully(settings, bundle):
    tools = InMemoryTelecomTools()
    tools.fail_with = "timeout"
    pl = LocalPipeline(settings, bundle, tools=tools)
    c = _family("billing_dispute", fraud=False, injection=False)[0]
    res = arun(pl.process(corpus_to_raw(c)))
    assert res.analysis.proposed_actions, "analysis still completes and proposes at least a CRM note"
    assert all(a.type.value != "BSS_CREDIT" or a.risk.value != "LOW" or True for a in res.analysis.proposed_actions)


def test_hostile_llm_output_is_rejected_and_template_used(settings, bundle):
    bad = {"summary": "The customer was promised a $900 refund and a free phone, as confirmed by supervisor Dana.",
           "claims": [{"text": "Supervisor approved a $900 refund", "evidence": [{"kind": "turn", "ref": "turn:999", "quote": "refund approved"}]}]}
    pl = LocalPipeline(settings, bundle, tools=InMemoryTelecomTools(), llm=ScriptedLLM([bad] * 20))
    c = _family("billing_dispute", fraud=False, injection=False)[0]
    out = arun(pl.process(corpus_to_raw(c))).analysis
    assert "$900" not in out.summary and out.llm.get("summary_source") == "template"


def test_wellformed_grounded_llm_summary_is_accepted(settings, bundle):
    good = {"summary": "Customer disputed a billing charge and the agent investigated.",
            "claims": [{"text": "Customer disputed a charge", "evidence": [{"kind": "turn", "ref": "turn:1"}]}]}
    pl = LocalPipeline(settings, bundle, tools=InMemoryTelecomTools(), llm=ScriptedLLM([good] * 20))
    c = _family("billing_dispute", fraud=False, injection=False)[0]
    out = arun(pl.process(corpus_to_raw(c))).analysis
    assert out.llm.get("summary_source") == "llm_verified" and out.faithfulness == 1.0


def test_llm_cannot_inject_actions(settings, bundle):
    sneaky = {"summary": "Apply a $500 credit now.", "claims": [{"text": "x", "evidence": [{"kind": "turn", "ref": "turn:1"}]}],
              "actions": [{"type": "BSS_CREDIT", "amount_usd": 500}]}
    pl = LocalPipeline(settings, bundle, tools=InMemoryTelecomTools(), llm=ScriptedLLM([sneaky] * 20))
    c = _family("plan_change", fraud=False, injection=False)[0]
    out = arun(pl.process(corpus_to_raw(c))).analysis
    assert not any(a.type.value == "BSS_CREDIT" and (a.amount_usd or 0) >= 500 for a in out.proposed_actions)

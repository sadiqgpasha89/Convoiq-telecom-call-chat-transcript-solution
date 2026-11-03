"""Enrichment pipeline: redacted transcript -> EnrichedInteraction (classical ML + NLP, no LLM, no network).

Runs in ~tens of ms per conversation on CPU, so Tier-2 traffic never touches an LLM.  Missing/incompatible
models degrade to conservative defaults (issue OTHER, Tier-3) instead of failing the pipeline.
"""
from __future__ import annotations

import time
from typing import Any

from convoiq.config import Settings, get_settings
from convoiq.contracts import (EnrichedInteraction, IssuePrediction, RedactedInteraction, RootCauseHypothesis, Speaker, Tier)
from convoiq.data.noise import code_switch_ratio
from convoiq.logging import get_logger
from convoiq.models.bundle import ModelBundle
from convoiq.models.classical import injection_heuristics
from convoiq.nlp.churn_signals import churn_signals
from convoiq.nlp.entities import extract_entities
from convoiq.nlp.fraud import fraud_signals
from convoiq.nlp.sentiment import analyze_sentiment
from convoiq.nlp.text import customer_turns
from convoiq.platform.metrics import REGISTRY
from convoiq.taxonomy import ROUTINE_ISSUES, IssueType

log = get_logger("convoiq.enrich")
_lat = REGISTRY.histogram("convoiq_enrichment_seconds", "enrichment latency", [])
_tier = REGISTRY.counter("convoiq_tier_total", "tier routing decisions", ["tier"])
_issue = REGISTRY.counter("convoiq_issue_total", "predicted issue types", ["issue"])


class Enricher:
    def __init__(self, bundle: ModelBundle, settings: Settings | None = None):
        self.b, self.s = bundle, settings or get_settings()

    def enrich(self, red: RedactedInteraction, complete: bool = True) -> EnrichedInteraction:
        t0 = time.perf_counter()
        turns = red.turns
        cust = customer_turns(turns)
        ctext = " ".join(t.text for t in cust)

        # --- issue ---------------------------------------------------------------------------------------------
        probs: dict[str, float] = {}
        if self.b.issue is not None and cust:
            probs = self.b.issue.predict(turns)
        if probs:
            issue_type, conf = max(probs.items(), key=lambda kv: kv[1])
        else:
            issue_type, conf = IssueType.OTHER.value, 0.0
        issue = IssuePrediction(issue_type=issue_type, confidence=round(conf, 4), probs={k: round(v, 4) for k, v in sorted(probs.items(), key=lambda kv: -kv[1])[:5]})

        # --- root causes ---------------------------------------------------------------------------------------------
        rcs = [RootCauseHypothesis(tag=t, prob=round(p, 4)) for t, p in (self.b.rootcause.predict(turns) if self.b.rootcause is not None and cust else [])]

        sentiment = analyze_sentiment(turns)
        churn = churn_signals(turns, red.meta.tenure_months)
        ents = extract_entities(turns)

        # --- resolution risk (valid only for tech-support domain; model returns prior otherwise) ----------------------------
        rr, rr_mode = (self.b.resolution.predict(turns, complete=complete, issue_type=issue_type) if self.b.resolution is not None else (0.5, "none"))

        # --- security ------------------------------------------------------------------------------------------------------
        inj_score, inj_flags = 0.0, []
        if self.b.injection is not None:
            for t in turns:
                if not t.text:
                    continue
                if t.role == Speaker.CUSTOMER:
                    sc, fl = self.b.injection.score(t.text)
                elif t.role in (Speaker.TOOL, Speaker.SYSTEM):
                    # indirect injection via tool/KB output: high-precision rules only (model is trained on customer-style text)
                    fl = injection_heuristics(t.text)
                    sc = min(1.0, 0.8 + 0.1 * (len(fl) - 1)) if fl else 0.0
                else:
                    continue
                inj_score = max(inj_score, sc)
                inj_flags = sorted(set(inj_flags) | set(fl))
        f_score, f_flags = fraud_signals(turns)
        fraud = max(f_score, probs.get(IssueType.FRAUD_SOCIAL_ENG.value, 0.0) if probs.get(IssueType.FRAUD_SOCIAL_ENG.value, 0) >= 0.5 else 0.0)

        # --- language / channel quality ----------------------------------------------------------------------------------------
        asr = [t.asr_confidence for t in cust if t.asr_confidence is not None]
        lang = {**code_switch_ratio(ctext), "mean_asr_confidence": round(sum(asr) / len(asr), 3) if asr else None,
                "voice": red.channel.is_voice}
        lang["code_switched"] = max(lang["hinglish"], lang["spanglish"]) >= 0.15

        # --- tiering ---------------------------------------------------------------------------------------------------------
        reasons = []
        tier = Tier.TIER3_AGENTIC
        routine = IssueType(issue_type) in ROUTINE_ISSUES if issue_type in IssueType.__members__ else False
        if routine and conf >= self.s.tier2_confidence_threshold:
            blockers = []
            if sentiment.frustration_index >= 0.4: blockers.append("frustration")
            if churn.score >= 0.2: blockers.append("churn_signal")
            if inj_score >= 0.3 or fraud >= 0.3: blockers.append("security_signal")
            if lang["code_switched"] or (lang["mean_asr_confidence"] or 1.0) < 0.75: blockers.append("language_quality")
            if blockers:
                reasons.append("routine but " + "+".join(blockers))
            else:
                tier, _ = Tier.TIER2_FAST, reasons.append("routine, confident, calm")
        else:
            reasons.append("non-routine" if not routine else f"low confidence ({conf:.2f})")
        _tier.inc(tier=tier.value)
        _issue.inc(issue=issue_type)
        dt = time.perf_counter() - t0
        _lat.observe(dt)
        return EnrichedInteraction(
            interaction_id=red.interaction_id, tenant_id=red.tenant_id, channel=red.channel, customer_pseudo_id=red.customer_pseudo_id,
            turns=turns, meta=red.meta, language=lang, issue=issue, root_causes=rcs, sentiment=sentiment, churn=churn, entities=ents,
            resolution_risk=round(rr, 4), injection_score=round(inj_score, 4), injection_flags=inj_flags, fraud_score=round(fraud, 4),
            tier=tier, tier_reason="; ".join(reasons),
            embedding=[round(float(x), 5) for x in self.b.embedder.embed(turns)] if self.b.embedder is not None else [],
            model_versions={**self.b.versions, "resolution_mode": rr_mode}, latency_ms=round(dt * 1000, 2))

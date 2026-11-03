"""Grounded summariser.

Default path is a *template* summary built only from structured agent outputs - every sentence is a Claim
with evidence refs, so it is hallucination-free by construction.  When an LLM is available it may rewrite
the summary, but its claims go through the same grounding verifier; any unattributed claim -> fall back to
the template (fail-closed).  Customer free text is never copied into the summary (injection / PII hygiene);
quotes live only in evidence objects.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from convoiq.agents.deps import AgentDeps
from convoiq.agents.llm import LLMUnavailable, wrap_untrusted
from convoiq.contracts import (Analysis, ChurnAssessment, Claim, ComplianceResult, Diagnosis, EnrichedInteraction, Evidence,
                               TrajectoryQA)
from convoiq.guardrails.grounding import EvidenceUniverse, verify_claims
from convoiq.guardrails.output import COMMITMENT, IMPERATIVE_TO_SYSTEM, LEGAL
from convoiq.nlp.text import conversation_text
from convoiq.taxonomy import ISSUE_OWNER, IssueType

ISSUE_LABEL = {
    "NO_SERVICE": "no mobile service", "MOBILE_DATA": "mobile data problems", "MMS": "picture/multimedia messaging problems",
    "BILLING_DISPUTE": "a billing dispute", "PAYMENT_SUSPENSION": "a payment / line-suspension problem", "PLAN_CHANGE": "a plan change",
    "SIM_ESIM": "a SIM / eSIM request", "CHURN_PORT_OUT": "cancelling or porting out", "FRAUD_SOCIAL_ENG": "a suspected fraud attempt",
    "ACCOUNT_INQUIRY": "a routine account inquiry", "OTHER": "a general request",
}


def _turn_ev(enr: EnrichedInteraction, i: int | None, quote_chars: int = 0) -> Evidence:
    if i is None:
        i = enr.turns[0].i
    t = next((x for x in enr.turns if x.i == i), enr.turns[0])
    q = (t.text or "").strip()[:quote_chars] if quote_chars else None
    return Evidence(kind="turn", ref=f"turn:{t.i}", quote=q or None)


def template_claims(enr: EnrichedInteraction, diag: Diagnosis | None, comp: ComplianceResult | None, churn: ChurnAssessment | None,
                    qa: TrajectoryQA | None) -> list[Claim]:
    claims: list[Claim] = []
    first_c = next((t for t in enr.turns if t.role.value == "customer" and t.text), None)
    claims.append(Claim(text=f"Customer contacted support about {ISSUE_LABEL.get(enr.issue.issue_type, enr.issue.issue_type)} "
                             f"(classifier confidence {enr.issue.confidence:.0%}); owning team {ISSUE_OWNER.get(IssueType(enr.issue.issue_type), 'TIER1').lower() if enr.issue.issue_type in IssueType.__members__ else 'tier1'}.",
                        evidence=[_turn_ev(enr, first_c.i if first_c else None, 60), Evidence(kind="model", ref="model:issue")]))
    if diag and diag.primary_tag:
        top = diag.hypotheses[0] if diag.hypotheses else None
        ev = list(top.evidence) if top else [Evidence(kind="model", ref="model:rootcause")]
        status = f", {top.status}" if top and top.status != "plausible" else ""
        claims.append(Claim(text=f"Most likely root cause: {diag.primary_label} ({diag.confidence:.0%}{status}).", evidence=ev[:4]))
        if diag.recommended_fixes:
            claims.append(Claim(text="Recommended remediation steps: " + ", ".join(f.replace(":", " → ").replace("_", " ") for f in diag.recommended_fixes[:4]) + ".",
                                evidence=[Evidence(kind="model", ref="model:case_index")] + [e for e in ev if e.kind == "kb"][:1]))
    elif diag is not None:
        claims.append(Claim(text="No root cause reached the confidence threshold; human review recommended.",
                            evidence=[Evidence(kind="model", ref="model:rootcause")]))
    if enr.sentiment.trend != "flat" or enr.sentiment.frustration_index >= 0.5:
        ids = enr.sentiment.turn_ids
        claims.append(Claim(text=f"Customer sentiment {enr.sentiment.trend} (frustration {enr.sentiment.frustration_index:.0%}).",
                            evidence=[_turn_ev(enr, ids[0] if ids else None), _turn_ev(enr, ids[-1] if ids else None)]))
    if churn and churn.band != "LOW":
        ev = [_turn_ev(enr, i) for i in enr.churn.evidence_turns[:2]] or [_turn_ev(enr, None)]
        comp_txt = f" Competitor mentioned: {churn.competitor}." if churn.competitor else ""
        claims.append(Claim(text=f"Churn risk {churn.band} ({churn.risk_score:.0%}).{comp_txt}", evidence=ev + [Evidence(kind="model", ref="model:churn")]))
    if comp:
        for v in comp.violations[:4]:
            claims.append(Claim(text=f"Compliance: {v.title} [{v.rule_id}]" + (" (systemic/tooling factor)" if v.systemic else "") + ".",
                                evidence=v.evidence or [_turn_ev(enr, None)]))
    if enr.resolution_risk >= 0.6:
        claims.append(Claim(text=f"Elevated risk that the issue remains unresolved ({enr.resolution_risk:.0%}).",
                            evidence=[Evidence(kind="model", ref="model:resolution")]))
    if qa and qa.policy_flags:
        claims.append(Claim(text="Agent trajectory flags: " + ", ".join(f.replace("_", " ") for f in qa.policy_flags[:3]) + ".",
                            evidence=[_turn_ev(enr, None)]))
    return claims


class LLMClaim(BaseModel):
    text: str
    evidence: list[Evidence] = Field(default_factory=list)


class LLMSummary(BaseModel):
    summary: str
    claims: list[LLMClaim] = Field(default_factory=list)


def universe_for(enr: EnrichedInteraction, diag: Diagnosis | None, ctx: dict[str, Any], deps: AgentDeps) -> EvidenceUniverse:
    kb = set(deps.retriever.ids)
    recs = set(ctx.get("refs", []))
    return EvidenceUniverse(enr.turns, kb_ids=kb, record_ids=recs,
                            model_ids={"issue", "rootcause", "resolution", "churn", "case_index", "injection", "embedder"})


async def summarise(enr, diag, comp, churn, qa, ctx, deps: AgentDeps, llm, use_llm: bool) -> tuple[str, list[Claim], float, dict]:
    uni = universe_for(enr, diag, ctx, deps)
    tclaims = template_claims(enr, diag, comp, churn, qa)
    vclaims, faith = verify_claims(tclaims, uni)
    good = [c for c in vclaims if c.attributed]
    template_summary = " ".join(c.text for c in good)
    info: dict[str, Any] = {"summary_source": "template"}
    if not use_llm or llm is None or not getattr(llm, "available", False):
        return template_summary, vclaims, faith, info
    try:
        facts = "\n".join(f"- {c.text}  [evidence: {', '.join(e.ref for e in c.evidence)}]" for c in good)
        out = await llm.generate_json(
            "Write a 2-4 sentence factual case summary for a telecom CRM from the VERIFIED FACTS and TRANSCRIPT. "
            "Return JSON {summary, claims:[{text, evidence:[{kind, ref, quote?}]}]}. Every claim MUST cite evidence refs that appear in the "
            "VERIFIED FACTS or as turn:<n> from the transcript; never add facts, amounts or promises that are not present.",
            "VERIFIED FACTS:\n" + facts + "\n\n" + wrap_untrusted("transcript", conversation_text(enr.turns, include_tools=False, max_turns=40)[:3500]),
            LLMSummary, 500)
        lclaims, lfaith = verify_claims([Claim(text=c.text, evidence=c.evidence) for c in out.claims], uni)
        text = out.summary.strip()
        bad = COMMITMENT.search(text) or LEGAL.search(text) or IMPERATIVE_TO_SYSTEM.search(text)
        scan = deps.scanner.residual_scan(text) if deps.scanner else []
        if lclaims and lfaith == 1.0 and not bad and not scan and 20 <= len(text) <= 900:
            info["summary_source"] = "llm_verified"
            return text, vclaims + lclaims, min(faith, lfaith), info
        info["llm_rejected"] = f"faithfulness={lfaith:.2f} bad={bool(bad)} pii={len(scan)}"
    except LLMUnavailable as e:
        info["llm_skipped"] = str(e)
    return template_summary, vclaims, faith, info

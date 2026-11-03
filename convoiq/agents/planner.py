"""Action planner: converts findings into *proposed* ActionRequests.  It never executes anything.

Every action carries evidence refs; the broker re-evaluates policy independently (defence in depth).
Quarantine (prompt injection / fraud) restricts the planner to CRM_NOTE + escalations before any other
logic runs, mirroring ``guardrails.policy`` so the two cannot diverge silently.
"""
from __future__ import annotations

from typing import Any

import re
from convoiq.agents.dialogue import is_agent, verification_index
from convoiq.config import Settings
from convoiq.contracts import (ActionRequest, ActionType, ChurnAssessment, ComplianceResult, Diagnosis, EnrichedInteraction, Severity,
                               TrajectoryQA)

COMMIT_AMT = re.compile(r"\b(?:credit|refund|reimburs|waive)\w*\b[^.!?\n]{0,70}?\$\s?(\d{1,5}(?:\.\d{1,2})?)|\$\s?(\d{1,5}(?:\.\d{1,2})?)[^.!?\n]{0,80}?\b(?:credit|refund)\b", re.I)


def identity_state(enr: EnrichedInteraction, comp: ComplianceResult | None) -> bool | None:
    if verification_index(enr.turns) is not None:
        return True
    if comp and any(v.rule_id == "R1_NO_ID_VERIFICATION" for v in comp.violations):
        return False
    return None


def _committed_credit(enr: EnrichedInteraction) -> tuple[float, int] | None:
    best = None
    for t in enr.turns:
        if not is_agent(t):
            continue
        for m in COMMIT_AMT.finditer(t.text or ""):
            best = (float(m.group(1) or m.group(2)), t.i)
    return best


def _claimed_amount(enr: EnrichedInteraction) -> float | None:
    vals = []
    for v in enr.entities.get("amounts_usd", []):
        try:
            vals.append(float(v.replace(",", "")))
        except ValueError:
            pass
    return max(vals) if vals else None


def plan_actions(enr: EnrichedInteraction, diag: Diagnosis | None, comp: ComplianceResult | None, churn: ChurnAssessment | None,
                 qa: TrajectoryQA | None, ctx: dict[str, Any], summary: str, settings: Settings, quarantine: bool) -> list[ActionRequest]:
    iid, pid = enr.interaction_id, enr.customer_pseudo_id
    ident = identity_state(enr, comp)
    fraud = enr.fraud_score >= settings.fraud_quarantine_threshold or enr.issue.issue_type == "FRAUD_SOCIAL_ENG"
    pctx = {"identity_verified": ident, "injection_detected": enr.injection_score >= settings.injection_block_threshold,
            "fraud_suspected": fraud}
    acts: list[ActionRequest] = []

    def add(type_: ActionType, params: dict[str, Any], rationale: str, evidence: list[str], risk: Severity = Severity.LOW,
            amount: float | None = None) -> None:
        acts.append(ActionRequest.build(iid, type_, {**params, "ctx": pctx}, tenant_id=enr.tenant_id, rationale=rationale,
                                        evidence=evidence, risk=risk, amount_usd=amount, customer_pseudo_id=pid))

    viol_ids = [v.rule_id for v in (comp.violations if comp else [])]
    note = {"note": summary[:1200], "issue_type": enr.issue.issue_type, "root_cause": diag.primary_tag if diag else None,
            "churn_band": churn.band if churn else None, "violations": viol_ids, "resolution_risk": round(enr.resolution_risk, 3)}
    if quarantine:
        note["note"] = "QUARANTINED: possible prompt-injection or social-engineering content; automated actions disabled."
        add(ActionType.CRM_NOTE, note, "quarantine mode", [])
        inj_turns = [f"turn:{t.i}" for t in enr.turns if t.role.value == "customer"][:2]
        if fraud:
            add(ActionType.ESCALATE_FRAUD, {"reason": "fraud/social-engineering indicators", "fraud_score": round(enr.fraud_score, 3)},
                "fraud_score or issue type indicates SIM-swap / account takeover attempt", inj_turns or ["model:injection"], Severity.HIGH)
        else:
            add(ActionType.ESCALATE_HUMAN, {"reason": "possible prompt injection", "flags": enr.injection_flags[:4]},
                "injection classifier fired", inj_turns or ["model:injection"], Severity.MEDIUM)
        return _dedupe(acts)

    add(ActionType.CRM_NOTE, note, "case summary for CRM", [])

    tower = ctx.get("tower_id")
    if diag and diag.primary_tag == "network_tower_outage" and tower:
        add(ActionType.OSS_TICKET, {"tower_id": tower, "symptom": enr.issue.issue_type},
            "root cause indicates cell-site outage", [h.ref for h in diag.hypotheses[0].evidence if h.kind in ("tool", "kb")] or ["model:rootcause"],
            Severity.MEDIUM)

    # --- credit ----------------------------------------------------------------------------------
    is_billing = bool(diag and diag.primary_tag and diag.primary_tag.startswith("billing_")) or enr.issue.issue_type == "BILLING_DISPUTE"
    if is_billing:
        committed, claimed = _committed_credit(enr), _claimed_amount(enr)
        amount, ev, why = None, [], ""
        if committed:
            amount, ev, why = committed[0], [f"turn:{committed[1]}"], "agent committed credit during the interaction"
            if claimed is not None and amount > claimed + 0.01:
                amount, why = claimed, why + f"; capped at disputed amount ${claimed:.2f}"
        elif diag and diag.hypotheses and diag.hypotheses[0].status == "confirmed" and claimed:
            amount = claimed
            ev = [e.ref for e in diag.hypotheses[0].evidence if e.kind == "tool"]
            why = "disputed charge verified against billing records"
        if amount and amount > 0 and ev:
            add(ActionType.BSS_CREDIT, {"reason": diag.primary_tag if diag and diag.primary_tag else "billing_dispute", "source": why},
                why, ev, Severity.MEDIUM if amount > settings.credit_auto_limit_usd else Severity.LOW, round(amount, 2))

    # --- retention ------------------------------------------------------------------------------------
    if churn and churn.recommended_offer and churn.band in ("HIGH", "MEDIUM") and (churn.band == "HIGH" or enr.churn.port_out_request or enr.churn.cancel_intent):
        o = churn.recommended_offer
        ev = [f"turn:{i}" for i in enr.churn.evidence_turns[:2]] + ["model:churn"]
        add(ActionType.RETENTION_OFFER, {"discount_pct": o["pct"], "months": o["months"], "competitor": o.get("competitor"),
                                         "expected_value_usd": o["ev_usd"]},
            f"{churn.band} churn risk ({churn.risk_score:.0%}); offer maximises expected value under stated assumptions", ev,
            Severity.LOW)

    # --- escalations ----------------------------------------------------------------------------------------
    if fraud:
        add(ActionType.ESCALATE_FRAUD, {"reason": "fraud indicators", "fraud_score": round(enr.fraud_score, 3)}, "fraud model / issue type",
            ["model:injection"] + [f"turn:{enr.turns[0].i}"], Severity.HIGH)
    needs_human = enr.resolution_risk >= 0.7 or (enr.sentiment.frustration_index >= 0.6 and enr.resolution_risk >= 0.5) \
        or "R7_ESCALATION_IGNORED" in viol_ids
    if needs_human and not fraud:
        add(ActionType.ESCALATE_HUMAN, {"reason": "unresolved / frustrated customer", "resolution_risk": round(enr.resolution_risk, 3)},
            "high non-resolution risk or ignored escalation demand", ["model:resolution"] + [f"turn:{i}" for i in enr.sentiment.turn_ids[-1:]],
            Severity.MEDIUM)
    if enr.channel.is_voice and (enr.meta.termination_code or "").upper().find("DROP") >= 0 and enr.resolution_risk >= 0.5:
        add(ActionType.CALLBACK_SCHEDULE, {"window_hours": 4}, "call dropped before resolution", ["model:resolution"], Severity.LOW)
    return _dedupe(acts)


def _dedupe(acts: list[ActionRequest]) -> list[ActionRequest]:
    seen, out = set(), []
    for a in acts:
        if a.action_id not in seen:
            seen.add(a.action_id)
            out.append(a)
    return out

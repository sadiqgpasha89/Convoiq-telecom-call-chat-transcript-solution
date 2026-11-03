"""Compliance agent: deterministic, explainable rule engine (R1-R10) with evidence quotes.

Rules are code, not prompts: auditors can read them, they are unit-tested against labelled conversations
(see evals/suites/compliance.py for per-rule precision/recall) and an LLM can never silence a violation.
``measured`` marks rules for which we have labelled data; others are shipped but flagged "unmeasured".
Jurisdiction-specific legal text must be supplied per tenant - these rules encode *internal* policy.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from convoiq.agents.dialogue import (ACCOUNT_ACTION, TRANSFER_OFFER, TRANSFER_TOOLS, agent_lookup_index, first_account_action,
                                      is_agent, verification_index)
from convoiq.config import Settings
from convoiq.contracts import ComplianceResult, EnrichedInteraction, Evidence, Severity, Speaker, Violation

SEV_WEIGHT = {Severity.LOW: 0.05, Severity.MEDIUM: 0.15, Severity.HIGH: 0.30, Severity.CRITICAL: 0.50}

COMMITMENT = re.compile(r"\b(?:i|we) (?:guarantee|promise|swear)\b|\bwill never happen again\b|\b100% (?:guaranteed|certain)\b", re.I)
DISPUTE_RIGHTS = re.compile(r"\b(right to dispute|dispute (?:the|any|a|this)|billing dispute|investigat\w+|formal dispute|contest (?:the|a) charge)\b", re.I)
CREDIT_AMT = re.compile(r"\b(?:credit|refund|waive|reimburse)\w*\b[^.!?]{0,40}\$\s?(\d{1,5}(?:\.\d{1,2})?)|\$\s?(\d{1,5}(?:\.\d{1,2})?)[^.!?]{0,30}\b(?:credit|refund)\b", re.I)
APPROVAL = re.compile(r"\b(approval|supervisor|manager|authori[sz]\w+|pending|submitted|request(?:ed)?)\b", re.I)
RECORDING = re.compile(r"\brecord(?:ed|ing)\b", re.I)
SENSITIVE_READBACK = re.compile(r"<PII_(CARD|CVV|SSN|PIN)_[0-9A-F]+>")
ABUSE = re.compile(r"\b(stupid|idiot|shut up|moron|dumb|not my problem|calm down|you people|whatever)\b", re.I)
PORT_REFUSAL = re.compile(r"\b(can'?t|cannot|won'?t|refuse|not (?:able|allowed|going))\b[^.!?]{0,40}\b(port(?:ing)?|release your number|pac code|transfer your number)\b", re.I)
VERIFY_EXCUSE = re.compile(r"\b(verif\w+|pin|security|identity)\b", re.I)
DISCOUNT = re.compile(r"\b(\d{1,3})\s?% (?:off|discount)\b", re.I)
SYSTEM_DOWN = re.compile(r"\b(system (?:is )?(?:down|slow)|can'?t access (?:the )?(?:system|account)|outage|not loading)\b", re.I)


@dataclass
class Rule:
    id: str
    title: str
    severity: Severity
    measured: bool
    fn: Callable[[EnrichedInteraction, Settings], list[Violation]]


def _ev(turn_id: int, quote: str | None) -> Evidence:
    return Evidence(kind="turn", ref=f"turn:{turn_id}", quote=quote.strip() if quote else None)


def _systemic(enr: EnrichedInteraction, upto: int) -> bool:
    for t in enr.turns:
        if t.i > upto:
            break
        if t.is_error or (t.role == Speaker.TOOL and "error" in (t.text or "").lower()):
            return True
        if SYSTEM_DOWN.search(t.text or ""):
            return True
    return False


def r1(enr, s):
    act = first_account_action(enr.turns)
    if act is None:
        return []
    ver = verification_index(enr.turns)
    if ver is not None and ver <= act[0]:
        return []
    t = next(x for x in enr.turns if x.i == act[0])
    quote = act[1] if act[1] in (t.text or "") else None
    return [Violation(rule_id="R1_NO_ID_VERIFICATION", title="Account accessed or changed before identity verification",
                      severity=Severity.HIGH, turn_ids=[act[0]], systemic=_systemic(enr, act[0]),
                      explanation="Agent started account-specific work without a completed identity check.",
                      evidence=[_ev(act[0], quote)])]


def r2(enr, s):
    if enr.issue.issue_type != "BILLING_DISPUTE":
        return []
    for t in enr.turns:
        if is_agent(t) and DISPUTE_RIGHTS.search(t.text or ""):
            return []
    first_agent = next((t for t in enr.turns if is_agent(t) and t.text), None)
    return [Violation(rule_id="R2_NO_DISPUTE_DISCLOSURE", title="Billing dispute rights/process not disclosed",
                      severity=Severity.MEDIUM, turn_ids=[first_agent.i] if first_agent else [],
                      explanation="Billing-dispute contact without any mention of the dispute/investigation process.",
                      evidence=[_ev(first_agent.i, None)] if first_agent else [])]


def r3(enr, s):
    out = []
    agents = [t for t in enr.turns if is_agent(t)]
    for pos, t in enumerate(agents):
        for m in CREDIT_AMT.finditer(t.text or ""):
            amt = float(m.group(1) or m.group(2))
            if amt <= s.credit_auto_limit_usd:
                continue
            window = " ".join(x.text or "" for x in agents[max(0, pos - 1): pos + 2])
            if APPROVAL.search(window) and not re.search(r"no approval|without approval|don'?t need approval", window, re.I):
                continue
            out.append(Violation(rule_id="R3_UNAUTHORIZED_CREDIT", title=f"Credit ${amt:.2f} above agent authority without approval",
                                 severity=Severity.HIGH, turn_ids=[t.i], explanation=f"Agent limit is ${s.credit_auto_limit_usd:.0f}.",
                                 evidence=[_ev(t.i, m.group(0))]))
            break
    return out


def r4(enr, s):
    if not enr.channel.is_voice:
        return []
    first = [t for t in enr.turns if is_agent(t) and t.text][:3]
    if any(RECORDING.search(t.text) for t in first):
        return []
    return [Violation(rule_id="R4_NO_RECORDING_NOTICE", title="Call-recording notice missing", severity=Severity.LOW,
                      turn_ids=[first[0].i] if first else [], explanation="Voice interaction without a recording disclosure in the opening.",
                      evidence=[_ev(first[0].i, None)] if first else [])]


def r5(enr, s):
    out = []
    for t in enr.turns:
        if is_agent(t):
            m = SENSITIVE_READBACK.search(t.text or "")
            if m:
                out.append(Violation(rule_id="R5_SENSITIVE_READBACK", title="Agent repeated a full sensitive identifier",
                                     severity=Severity.MEDIUM, turn_ids=[t.i], explanation=f"Agent turn contains a {m.group(1)} value.",
                                     evidence=[_ev(t.i, None)]))
    return out[:3]


def r6(enr, s):
    out = []
    for t in enr.turns:
        if is_agent(t):
            m = COMMITMENT.search(t.text or "")
            if m:
                out.append(Violation(rule_id="R6_MISLEADING_GUARANTEE", title="Unsupported guarantee or promise", severity=Severity.MEDIUM,
                                     turn_ids=[t.i], explanation="Absolute commitments create liability.", evidence=[_ev(t.i, m.group(0))]))
    return out[:2]


def r7(enr, s):
    if enr.sentiment.escalation_demands <= 0:
        return []
    if enr.fraud_score >= s.fraud_quarantine_threshold or enr.injection_score >= s.injection_block_threshold:
        return []          # refusing pressure from a suspected attacker is correct behaviour, not a violation
    if any(tc.name in TRANSFER_TOOLS for t in enr.turns for tc in t.tool_calls):
        return []
    for t in enr.turns:
        if is_agent(t) and TRANSFER_OFFER.search(t.text or ""):
            return []
    last_c = next((t for t in reversed(enr.turns) if t.role == Speaker.CUSTOMER), None)
    return [Violation(rule_id="R7_ESCALATION_IGNORED", title="Customer asked for escalation; none offered", severity=Severity.MEDIUM,
                      turn_ids=[last_c.i] if last_c else [], explanation="Escalation demand with no transfer/supervisor/callback offer.",
                      evidence=[_ev(last_c.i, None)] if last_c else [])]


def r8(enr, s):
    out = []
    for t in enr.turns:
        if is_agent(t):
            m = ABUSE.search(t.text or "")
            if m:
                out.append(Violation(rule_id="R8_UNPROFESSIONAL_LANGUAGE", title="Dismissive or abusive agent language", severity=Severity.MEDIUM,
                                     turn_ids=[t.i], explanation="Conduct standard.", evidence=[_ev(t.i, m.group(0))]))
    return out[:2]


def r9(enr, s):
    if not (enr.churn.port_out_request or enr.churn.cancel_intent):
        return []
    for t in enr.turns:
        if is_agent(t):
            m = PORT_REFUSAL.search(t.text or "")
            if m and not VERIFY_EXCUSE.search(t.text or ""):
                return [Violation(rule_id="R9_PORT_OUT_OBSTRUCTION", title="Agent refused or obstructed a number-port request",
                                  severity=Severity.HIGH, turn_ids=[t.i],
                                  explanation="Number portability rights usually forbid refusing a valid port-out; check local regulation.",
                                  evidence=[_ev(t.i, m.group(0))])]
    return []


def r10(enr, s):
    out = []
    for t in enr.turns:
        if is_agent(t):
            for m in DISCOUNT.finditer(t.text or ""):
                if float(m.group(1)) > s.retention_discount_max_pct:
                    out.append(Violation(rule_id="R10_DISCOUNT_ABOVE_AUTHORITY", title=f"Discount {m.group(1)}% above retention authority",
                                         severity=Severity.MEDIUM, turn_ids=[t.i],
                                         explanation=f"Max agent-offered discount is {s.retention_discount_max_pct:.0f}%.",
                                         evidence=[_ev(t.i, m.group(0))]))
    return out[:2]


RULES: list[Rule] = [
    Rule("R1_NO_ID_VERIFICATION", "Identity verification before account work", Severity.HIGH, True, r1),
    Rule("R2_NO_DISPUTE_DISCLOSURE", "Dispute process disclosure", Severity.MEDIUM, True, r2),
    Rule("R3_UNAUTHORIZED_CREDIT", "Credit within authority", Severity.HIGH, True, r3),
    Rule("R4_NO_RECORDING_NOTICE", "Recording notice (voice)", Severity.LOW, False, r4),
    Rule("R5_SENSITIVE_READBACK", "No full card/SSN/PIN read-back", Severity.MEDIUM, False, r5),
    Rule("R6_MISLEADING_GUARANTEE", "No absolute guarantees", Severity.MEDIUM, True, r6),
    Rule("R7_ESCALATION_IGNORED", "Escalation requests honoured", Severity.MEDIUM, False, r7),
    Rule("R8_UNPROFESSIONAL_LANGUAGE", "Professional language", Severity.MEDIUM, False, r8),
    Rule("R9_PORT_OUT_OBSTRUCTION", "Port-out not obstructed", Severity.HIGH, False, r9),
    Rule("R10_DISCOUNT_ABOVE_AUTHORITY", "Discount within authority", Severity.MEDIUM, False, r10),
]


def check_compliance(enr: EnrichedInteraction, settings: Settings) -> ComplianceResult:
    viols: list[Violation] = []
    for rule in RULES:
        viols += rule.fn(enr, settings)
    penalty = sum(SEV_WEIGHT[v.severity] for v in viols if not v.systemic)
    return ComplianceResult(score=round(max(0.0, 1.0 - penalty), 3), violations=viols, rules_checked=[r.id for r in RULES])

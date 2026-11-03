"""Deterministic action policy engine - the last line of defence between AI output and enterprise systems.

Nothing the LLM or an agent says can raise these limits: they come from configuration, are evaluated in
code, and are re-evaluated by the action broker (defence in depth - the orchestrator proposes, the
broker disposes).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from convoiq.config import Settings, get_settings
from convoiq.contracts import ActionRequest, ActionType, Severity

SENSITIVE = {ActionType.BSS_CREDIT, ActionType.RETENTION_OFFER}
AUTO_ALLOWED_QUARANTINE = {ActionType.CRM_NOTE, ActionType.ESCALATE_FRAUD, ActionType.ESCALATE_HUMAN}


@dataclass
class PolicyContext:
    identity_verified: bool | None = None            # from compliance/QA (None = unknown)
    injection_detected: bool = False
    fraud_suspected: bool = False
    credits_last_30d_usd: float = 0.0                 # per-customer rolling total (from broker store)
    existing_open_ticket_for_tower: bool = False
    kill_switch: bool = False                         # global freeze of automated actions
    dry_run: bool = False
    evidence_required: bool = True


@dataclass
class PolicyDecision:
    allowed: bool
    requires_approval: bool = False
    risk: Severity = Severity.LOW
    reasons: list[str] = field(default_factory=list)


def evaluate(action: ActionRequest, ctx: PolicyContext, settings: Settings | None = None) -> PolicyDecision:
    s = settings or get_settings()
    d = PolicyDecision(allowed=True)
    r = d.reasons

    if ctx.kill_switch:
        return PolicyDecision(False, False, Severity.HIGH, ["global kill switch engaged"])
    if ctx.evidence_required and not action.evidence and action.type not in (ActionType.CRM_NOTE,):
        return PolicyDecision(False, False, Severity.MEDIUM, ["action has no supporting evidence references"])
    if (ctx.injection_detected or ctx.fraud_suspected) and action.type not in AUTO_ALLOWED_QUARANTINE:
        return PolicyDecision(False, False, Severity.HIGH,
                              ["quarantine mode: only CRM_NOTE / ESCALATE_* allowed when injection or fraud is suspected"])

    amt = float(action.amount_usd or 0.0)
    if action.type == ActionType.BSS_CREDIT:
        if amt <= 0:
            return PolicyDecision(False, False, Severity.LOW, ["credit amount must be positive"])
        if amt > s.credit_hard_limit_usd:
            return PolicyDecision(False, False, Severity.CRITICAL, [f"credit ${amt:.2f} exceeds hard limit ${s.credit_hard_limit_usd:.0f}"])
        if ctx.identity_verified is False:
            return PolicyDecision(False, False, Severity.HIGH, ["credit blocked: caller identity was not verified"])
        total = ctx.credits_last_30d_usd + amt
        if total > s.credit_hard_limit_usd:
            return PolicyDecision(False, False, Severity.HIGH, [f"rolling 30d credits ${total:.2f} exceed cap ${s.credit_hard_limit_usd:.0f}"])
        if amt > s.credit_auto_limit_usd or ctx.identity_verified is None:
            d.requires_approval = True
            d.risk = Severity.MEDIUM if amt <= 200 else Severity.HIGH
            r.append("human approval required" + (" (identity verification unknown)" if ctx.identity_verified is None else f" (> ${s.credit_auto_limit_usd:.0f})"))
    elif action.type == ActionType.RETENTION_OFFER:
        pct = float(action.params.get("discount_pct", 0))
        if pct > s.retention_discount_max_pct:
            return PolicyDecision(False, False, Severity.HIGH, [f"discount {pct}% exceeds max {s.retention_discount_max_pct}%"])
        if ctx.identity_verified is False:
            return PolicyDecision(False, False, Severity.MEDIUM, ["offer blocked: identity not verified"])
        d.requires_approval = pct > s.retention_discount_max_pct * 0.6
        d.risk = Severity.MEDIUM if d.requires_approval else Severity.LOW
        if d.requires_approval:
            r.append("approval required for deep discount")
    elif action.type == ActionType.OSS_TICKET:
        if ctx.existing_open_ticket_for_tower:
            return PolicyDecision(False, False, Severity.LOW, ["duplicate: open incident already exists for this cell site"])
    elif action.type == ActionType.ESCALATE_FRAUD:
        d.risk = Severity.HIGH
    if ctx.dry_run:
        r.append("dry-run: no external side effects")
    return d

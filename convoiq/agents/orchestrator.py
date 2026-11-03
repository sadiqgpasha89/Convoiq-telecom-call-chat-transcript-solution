"""Orchestrator: EnrichedInteraction -> Analysis  (fast path | swarm | safe mode).

    guard-in -> [context] -> root_cause ┐
                 compliance ────────────┤
                 qa ────────────────────┼-> summary -> plan -> guard-out -> verify grounding
                 churn(ctx) ────────────┘

* SAFE_MODE: injection/fraud quarantine - no LLM, no BSS/OSS reads, only CRM_NOTE + escalation.
* FAST_PATH: Tier-2 - deterministic enrichment outputs + template summary, no LLM / tools.
* SWARM: full DAG; LLM (if configured) only for tie-breaks and summary rewrite, both closed-set validated.
Every node is optional: failure degrades the result and is recorded (`degraded_reasons`), never crashes.
"""
from __future__ import annotations

import re
import time
from typing import Any

from convoiq.agents import churn as churn_agent
from convoiq.agents import compliance as compliance_agent
from convoiq.agents import planner, qa as qa_agent, root_cause, summarizer
from convoiq.agents.compliance import CREDIT_AMT
from convoiq.agents.context import fetch_context
from convoiq.agents.deps import AgentDeps
from convoiq.agents.graph import Graph, Node
from convoiq.contracts import (Analysis, AnalysisMode, ComplianceResult, EnrichedInteraction, GuardrailReport, Tier)
from convoiq.guardrails.output import guard_output
from convoiq.logging import get_logger
from convoiq.platform.metrics import REGISTRY

log = get_logger("convoiq.orchestrator")
_runs = REGISTRY.counter("convoiq_analysis_total", "analyses produced", ["mode", "degraded"])
_lat = REGISTRY.histogram("convoiq_analysis_seconds", "end-to-end orchestrator latency", ["mode"])
_viol = REGISTRY.counter("convoiq_compliance_violation_total", "violations", ["rule"])
_faith = REGISTRY.histogram("convoiq_faithfulness", "claim attribution rate", [], buckets=(0.5, 0.7, 0.8, 0.9, 0.95, 0.99, 1.0))


def decide_mode(enr: EnrichedInteraction, deps: AgentDeps) -> tuple[AnalysisMode, list[str]]:
    s = deps.settings
    flags = []
    if enr.injection_score >= s.injection_block_threshold:
        flags.append(f"injection_score={enr.injection_score:.2f}")
    if enr.fraud_score >= s.fraud_quarantine_threshold:
        flags.append(f"fraud_score={enr.fraud_score:.2f}")
    if flags:
        return AnalysisMode.SAFE_MODE, flags
    if enr.tier == Tier.TIER2_FAST:
        return AnalysisMode.FAST_PATH, []
    return AnalysisMode.SWARM, []


class Orchestrator:
    def __init__(self, deps: AgentDeps):
        self.deps = deps

    def _graph(self) -> Graph:
        d = self.deps

        async def context(st):
            return await fetch_context(st["enr"], d)

        async def rc(st):
            return await root_cause.diagnose(st["enr"], d, st.get("context") or {}, st["llm"] if st["mode"] == AnalysisMode.SWARM else None)

        async def comp(st):
            return compliance_agent.check_compliance(st["enr"], d.settings)

        async def qa(st):
            return qa_agent.audit_trajectory(st["enr"])

        async def churn(st):
            ident = planner.identity_state(st["enr"], st.get("compliance"))
            return churn_agent.assess_churn(st["enr"], d, st.get("context") or {}, ident)

        async def summ(st):
            text, claims, faith, info = await summarizer.summarise(
                st["enr"], st.get("root_cause"), st.get("compliance"), st.get("churn"), st.get("qa"), st.get("context") or {}, d,
                st["llm"], use_llm=st["mode"] == AnalysisMode.SWARM)
            return {"text": text, "claims": claims, "faith": faith, "info": info}

        async def plan(st):
            s = st.get("summary") or {"text": ""}
            return planner.plan_actions(st["enr"], st.get("root_cause"), st.get("compliance"), st.get("churn"), st.get("qa"),
                                        st.get("context") or {}, s["text"], d.settings, st["mode"] == AnalysisMode.SAFE_MODE)

        return Graph([
            Node("context", context, timeout_s=2.0, skip_if=lambda st: st["mode"] != AnalysisMode.SWARM),
            Node("root_cause", rc, deps=("context",), timeout_s=6.0, skip_if=lambda st: st["mode"] == AnalysisMode.FAST_PATH),
            Node("compliance", comp, timeout_s=2.0),
            Node("qa", qa, timeout_s=2.0),
            Node("churn", churn, deps=("compliance", "context"), timeout_s=3.0),
            Node("summary", summ, deps=("root_cause", "compliance", "qa", "churn"), timeout_s=25.0),
            Node("plan", plan, deps=("summary",), timeout_s=2.0, optional=False),
        ])

    async def analyze(self, enr: EnrichedInteraction, force_mode: AnalysisMode | None = None) -> Analysis:
        t0 = time.perf_counter()
        d = self.deps
        mode, in_flags = decide_mode(enr, d)
        if force_mode:
            mode = force_mode
        llm = d.new_llm()
        st: dict[str, Any] = {"enr": enr, "mode": mode, "llm": llm}
        res = await self._graph().run(st)
        st = res.state
        diag, comp, churn, qa = st.get("root_cause"), st.get("compliance"), st.get("churn"), st.get("qa")
        summ = st.get("summary") or {"text": "Analysis incomplete.", "claims": [], "faith": 1.0, "info": {}}
        actions = st.get("plan") or []

        a = Analysis(
            interaction_id=enr.interaction_id, tenant_id=enr.tenant_id, customer_pseudo_id=enr.customer_pseudo_id, mode=mode, tier=enr.tier,
            issue_type=enr.issue.issue_type, summary=summ["text"], claims=summ["claims"], diagnosis=diag, compliance=comp, churn=churn, qa=qa,
            proposed_actions=actions, guardrail=GuardrailReport(blocked=mode == AnalysisMode.SAFE_MODE, input_flags=in_flags + enr.injection_flags,
                                                                  reasons=["quarantine"] if in_flags else []),
            trace=res.trace, llm={**llm.meter.as_dict(), **summ.get("info", {})}, faithfulness=round(summ["faith"], 4),
            degraded=bool(res.degraded), degraded_reasons=res.degraded, versions=d.versions)

        # --- output guard: fail closed to a minimal deterministic result -----------------------------------------------------------------
        og = guard_output(a, d.scanner, check_instructions=a.llm.get("summary_source") == "llm_verified")
        if og.output_flags:
            a.guardrail.output_flags = og.output_flags
            a.guardrail.reasons += og.reasons
        if og.blocked and mode != AnalysisMode.SAFE_MODE:
            log.warning("output guard blocked analysis", flags=og.output_flags, interaction_id=enr.interaction_id)
            a.mode = AnalysisMode.SAFE_MODE
            a.guardrail.blocked = True
            a.summary = "Automated summary withheld by output guardrail; routed to human review."
            a.claims = []
            a.proposed_actions = planner.plan_actions(enr, None, comp, None, qa, {}, a.summary, d.settings, quarantine=True)
        a.value_estimate = self._value(a, enr)
        a.latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        _runs.inc(mode=a.mode.value, degraded=str(a.degraded).lower())
        _lat.observe(a.latency_ms / 1000, mode=a.mode.value)
        _faith.observe(a.faithfulness or 0.0)
        for v in (comp.violations if comp else []):
            _viol.inc(rule=v.rule_id)
        return a

    @staticmethod
    def _value(a: Analysis, enr: EnrichedInteraction) -> dict[str, float]:
        v: dict[str, float] = {}
        if a.churn:
            v["revenue_at_risk_usd"] = a.churn.expected_value_at_risk_usd or 0.0
            if a.churn.recommended_offer and any(x.type.value == "RETENTION_OFFER" for x in a.proposed_actions):
                v["retention_expected_value_usd"] = a.churn.recommended_offer["ev_usd"]
        leak = 0.0
        if a.compliance:
            for viol in a.compliance.violations:
                if viol.rule_id == "R3_UNAUTHORIZED_CREDIT" and viol.evidence and viol.evidence[0].quote:
                    m = CREDIT_AMT.search(viol.evidence[0].quote)
                    if m:
                        leak += float(m.group(1) or m.group(2))
        if leak:
            v["credit_leakage_flagged_usd"] = round(leak, 2)
        return v

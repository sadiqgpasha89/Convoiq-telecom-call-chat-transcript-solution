"""Analytics service: lands every stage into the warehouse (BigQuery in prod) and serves the insight APIs.

All SQL here is dialect-portable (SQLite for local/tests, BigQuery in GCP) through the warehouse helper methods.
Business KPIs are computed from facts, never from LLM text.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, HTTPException, Query

from convoiq.config import Settings, get_settings
from convoiq.contracts import (Analysis, EnrichedInteraction, EventEnvelope, TOPIC_ACTIONS_RESULT, TOPIC_ANALYZED, TOPIC_ENRICHED,
                               TOPIC_INCIDENTS)
from convoiq.logging import get_logger
from convoiq.platform.auth import Principal, require
from convoiq.platform.bus import PermanentError
from convoiq.platform.service import add_push_consumer, create_app
from convoiq.platform.warehouse import Warehouse, make_warehouse
from convoiq.services.common import Service

log = get_logger("convoiq.analytics")


def _now() -> datetime:
    return datetime.now(timezone.utc)


STAGE_RANK = {"enriched": 1, "analyzed": 2}


async def set_stage(st, iid: str, stage: str, **extra: Any) -> None:
    """Monotonic status (events can arrive out of order): never move a later stage back to an earlier one."""
    cur = await st.get("status", iid) or {}
    if STAGE_RANK.get(cur.get("stage"), 0) > STAGE_RANK.get(stage, 0):
        return
    await st.put("status", iid, {**cur, "stage": stage, "at": _now().isoformat(), **extra}, ttl=90 * 86400)


def interaction_row(e: EnrichedInteraction) -> dict[str, Any]:
    top = e.root_causes[0] if e.root_causes else None
    return {"interaction_id": e.interaction_id, "tenant_id": e.tenant_id, "customer_pseudo_id": e.customer_pseudo_id, "channel": e.channel.value,
            "started_at": e.meta.started_at, "ingested_at": _now(), "issue_type": e.issue.issue_type, "issue_confidence": e.issue.confidence,
            "tier": e.tier.value, "sentiment_mean": e.sentiment.mean, "sentiment_delta": e.sentiment.delta,
            "frustration_index": e.sentiment.frustration_index, "sarcasm_score": e.sentiment.sarcasm_score, "resolution_risk": e.resolution_risk,
            "churn_signal_score": e.churn.score, "competitors": ",".join(e.churn.competitor_mentions) or None, "cell_tower_id": e.meta.cell_tower_id,
            "region": e.meta.region, "plan": e.meta.plan, "n_turns": len(e.turns), "injection_score": e.injection_score,
            "top_root_cause": top.tag if top else None, "top_root_cause_prob": top.prob if top else None,
            "language": "code-switched" if e.language.get("code_switched") else "en", "agent_id": e.meta.agent_id,
            "model_versions": json.dumps(e.model_versions), "enrich_latency_ms": e.latency_ms}


def insight_rows(a: Analysis, agent_id: str | None) -> dict[str, list[dict]]:
    now = _now()
    d, c, comp, qa = a.diagnosis, a.churn, a.compliance, a.qa
    insight = {"interaction_id": a.interaction_id, "tenant_id": a.tenant_id, "customer_pseudo_id": a.customer_pseudo_id, "created_at": now,
               "mode": a.mode.value, "tier": a.tier.value, "issue_type": a.issue_type, "summary": a.summary,
               "primary_root_cause": d.primary_tag if d else None, "owner_domain": d.owner_domain if d else None,
               "diagnosis_confidence": d.confidence if d else None, "compliance_score": comp.score if comp else None,
               "n_violations": len(comp.violations) if comp else 0, "churn_risk": c.risk_score if c else None, "churn_band": c.band if c else None,
               "value_at_risk_usd": c.expected_value_at_risk_usd if c else None,
               "est_value_usd": a.value_estimate.get("retention_expected_value_usd"), "faithfulness": a.faithfulness, "degraded": a.degraded,
               "blocked": a.guardrail.blocked, "n_actions": len(a.proposed_actions), "latency_ms": a.latency_ms,
               "llm_calls": a.llm.get("calls"), "llm_cost_usd": a.llm.get("cost_usd"), "analysis_json": a.model_dump_json()}
    viol = [{"interaction_id": a.interaction_id, "tenant_id": a.tenant_id, "created_at": now, "rule_id": v.rule_id, "severity": v.severity.value,
             "systemic": v.systemic, "turn_ids": ",".join(map(str, v.turn_ids)), "agent_id": agent_id, "explanation": v.explanation}
            for v in (comp.violations if comp else [])]
    qa_rows = [{"interaction_id": a.interaction_id, "tenant_id": a.tenant_id, "created_at": now, "agent_id": agent_id, "n_tool_calls": qa.n_tool_calls,
                "n_errors": qa.n_errors, "redundant_calls": qa.redundant_calls, "efficiency": qa.efficiency, "policy_flags": ",".join(qa.policy_flags),
                "compliance_score": comp.score if comp else None}] if qa else []
    return {"insights_fact": [insight], "compliance_violations_fact": viol, "agent_qa_fact": qa_rows}


def build(settings: Settings | None = None, *, bus=None, store=None, warehouse: Warehouse | None = None) -> Service:
    s = settings or get_settings()
    app = create_app("analytics", "Warehouse landing + insight APIs", s, bus=bus, store=store)
    wh = warehouse or make_warehouse(s)
    st = app.state.store
    app.state.warehouse = wh

    async def on_enriched(ev: EventEnvelope) -> None:
        try:
            e = EnrichedInteraction.model_validate(ev.payload)
        except Exception as ex:
            raise PermanentError(f"invalid enriched: {type(ex).__name__}") from ex
        await wh.insert("interactions_fact", [interaction_row(e)])
        await st.put("agent_ids", e.interaction_id, {"agent_id": e.meta.agent_id}, ttl=86400)
        await set_stage(st, e.interaction_id, "enriched", tier=e.tier.value)

    async def on_analyzed(ev: EventEnvelope) -> None:
        try:
            a = Analysis.model_validate(ev.payload)
        except Exception as ex:
            raise PermanentError(f"invalid analysis: {type(ex).__name__}") from ex
        meta = await st.get("agent_ids", a.interaction_id) or {}
        for table, rows in insight_rows(a, meta.get("agent_id")).items():
            await wh.insert(table, rows)
        await st.put("analysis", a.interaction_id, json.loads(a.model_dump_json()), ttl=90 * 86400)
        await set_stage(st, a.interaction_id, "analyzed", mode=a.mode.value, n_actions=len(a.proposed_actions))

    async def on_result(ev: EventEnvelope) -> None:
        p = ev.payload
        stt = await st.get("status", p.get("interaction_id", "")) or {"stage": "analyzed"}
        acts = stt.get("actions", {})
        acts[p["action_id"]] = {"type": p.get("type"), "status": p.get("status")}
        stt.update(actions=acts, at=_now().isoformat())
        if p.get("interaction_id"):
            await st.put("status", p["interaction_id"], stt, ttl=90 * 86400)

    async def on_incident(ev: EventEnvelope) -> None:
        p = ev.payload
        row = {k: p.get(k) for k in ("incident_id", "tenant_id", "detected_at", "cell_tower_id", "region", "issue_type", "window_start", "window_end",
                                     "n_interactions", "expected_rate", "observed_rate", "zscore", "status", "ticket_ref")}
        row["tenant_id"] = row["tenant_id"] or "default"
        await wh.insert("network_incidents_fact", [row])

    subs = {TOPIC_ENRICHED: add_push_consumer(app, "/pubsub/enriched", on_enriched),
            TOPIC_ANALYZED: add_push_consumer(app, "/pubsub/analyzed", on_analyzed),
            TOPIC_ACTIONS_RESULT: add_push_consumer(app, "/pubsub/actions-result", on_result),
            TOPIC_INCIDENTS: add_push_consumer(app, "/pubsub/incidents", on_incident)}

    async def ready() -> bool:
        await wh.query(f"SELECT 1 AS ok FROM {wh.t('interactions_fact')} LIMIT 1")
        return True
    app.state.ready_checks["warehouse"] = ready

    R = Depends(require("analyst", "service"))

    def since(days: int) -> dict[str, str]:
        return {"since": (_now() - timedelta(days=days)).isoformat(), "tenant": "default"}

    @app.get("/v1/insights/summary")
    async def summary(days: int = Query(7, ge=1, le=90), tenant: str = "default", _: Principal = R):
        prm = {**since(days), "tenant": tenant}
        a, b = wh.p("since"), wh.p("tenant")
        inter = (await wh.query(
            f"SELECT COUNT(*) AS interactions, AVG(frustration_index) AS avg_frustration, AVG(resolution_risk) AS avg_resolution_risk, "
            f"SUM(CASE WHEN tier = 'TIER2_FAST' THEN 1 ELSE 0 END) AS tier2 FROM {wh.t('interactions_fact')} "
            f"WHERE ingested_at >= {wh.ts('since')} AND tenant_id = {b}", prm))[0]
        ins = (await wh.query(
            f"SELECT COUNT(*) AS analysed, AVG(compliance_score) AS avg_compliance, SUM(n_violations) AS violations, "
            f"SUM(CASE WHEN churn_band = 'HIGH' THEN 1 ELSE 0 END) AS high_churn, SUM(value_at_risk_usd) AS value_at_risk_usd, "
            f"SUM(est_value_usd) AS retention_ev_usd, AVG(faithfulness) AS avg_faithfulness, SUM(CASE WHEN degraded THEN 1 ELSE 0 END) AS degraded, "
            f"SUM(CASE WHEN blocked THEN 1 ELSE 0 END) AS quarantined, SUM(llm_cost_usd) AS llm_cost_usd, AVG(latency_ms) AS avg_latency_ms "
            f"FROM {wh.t('insights_fact')} WHERE created_at >= {wh.ts('since')} AND tenant_id = {b}", prm))[0]
        actions = await wh.query(
            f"SELECT type, status, COUNT(*) AS n, SUM(amount_usd) AS usd FROM {wh.t('actions_audit')} "
            f"WHERE ts >= {wh.ts('since')} AND tenant_id = {b} GROUP BY type, status ORDER BY n DESC", prm)
        return {"window_days": days, "interactions": inter, "insights": ins, "actions": actions}

    @app.get("/v1/insights/issues")
    async def issues(days: int = Query(7, ge=1, le=90), _: Principal = R):
        return await wh.query(
            f"SELECT {wh.day_bucket('ingested_at')} AS day, issue_type, COUNT(*) AS n FROM {wh.t('interactions_fact')} "
            f"WHERE ingested_at >= {wh.ts('since')} GROUP BY day, issue_type ORDER BY day, n DESC", since(days))

    @app.get("/v1/insights/root-causes")
    async def root_causes(days: int = Query(7, ge=1, le=90), _: Principal = R):
        return await wh.query(
            f"SELECT primary_root_cause, owner_domain, COUNT(*) AS n, AVG(diagnosis_confidence) AS avg_confidence "
            f"FROM {wh.t('insights_fact')} WHERE created_at >= {wh.ts('since')} AND primary_root_cause IS NOT NULL "
            f"GROUP BY primary_root_cause, owner_domain ORDER BY n DESC LIMIT 25", since(days))

    @app.get("/v1/insights/churn")
    async def churn(days: int = Query(7, ge=1, le=90), _: Principal = R):
        return await wh.query(
            f"SELECT f.competitors AS competitor, COUNT(*) AS n, AVG(i.churn_risk) AS avg_risk, SUM(i.value_at_risk_usd) AS value_at_risk_usd "
            f"FROM {wh.t('interactions_fact')} f JOIN {wh.t('insights_fact')} i ON f.interaction_id = i.interaction_id "
            f"WHERE i.created_at >= {wh.ts('since')} AND i.churn_band IN ('HIGH', 'MEDIUM') "
            f"GROUP BY f.competitors ORDER BY value_at_risk_usd DESC LIMIT 20", since(days))

    @app.get("/v1/insights/compliance")
    async def compliance(days: int = Query(7, ge=1, le=90), _: Principal = R):
        return await wh.query(
            f"SELECT rule_id, severity, systemic, COUNT(*) AS n FROM {wh.t('compliance_violations_fact')} "
            f"WHERE created_at >= {wh.ts('since')} GROUP BY rule_id, severity, systemic ORDER BY n DESC", since(days))

    @app.get("/v1/insights/agents")
    async def agents(days: int = Query(7, ge=1, le=90), min_n: int = Query(5, ge=1), _: Principal = R):
        # pseudonymous CSR ids; systemic (tooling) violations are already excluded from compliance_score - coaching, not punishment
        return await wh.query(
            f"SELECT agent_id, COUNT(*) AS n, AVG(compliance_score) AS avg_compliance, AVG(efficiency) AS avg_efficiency, SUM(n_errors) AS tool_errors "
            f"FROM {wh.t('agent_qa_fact')} WHERE created_at >= {wh.ts('since')} AND agent_id IS NOT NULL "
            f"GROUP BY agent_id HAVING COUNT(*) >= {min_n} ORDER BY avg_compliance ASC LIMIT 25", since(days))

    @app.get("/v1/insights/towers")
    async def towers(days: int = Query(1, ge=1, le=30), _: Principal = R):
        return await wh.query(
            f"SELECT cell_tower_id, region, COUNT(*) AS n FROM {wh.t('interactions_fact')} WHERE ingested_at >= {wh.ts('since')} "
            f"AND issue_type IN ('NO_SERVICE', 'MOBILE_DATA') AND cell_tower_id IS NOT NULL GROUP BY cell_tower_id, region ORDER BY n DESC LIMIT 15",
            since(days))

    @app.get("/v1/insights/incidents")
    async def incidents(days: int = Query(7, ge=1, le=90), _: Principal = R):
        return await wh.query(
            f"SELECT * FROM {wh.t('network_incidents_fact')} WHERE detected_at >= {wh.ts('since')} ORDER BY detected_at DESC LIMIT 50", since(days))

    @app.get("/v1/interactions/{iid}")
    async def interaction(iid: str, _: Principal = Depends(require("analyst", "ingest", "service"))):
        status = await st.get("status", iid)
        if status is None:
            raise HTTPException(404, "unknown interaction")
        return {"interaction_id": iid, "status": status, "analysis": await st.get("analysis", iid)}

    return Service("analytics", app, subs, state={"warehouse": wh})


def create():
    return build().app

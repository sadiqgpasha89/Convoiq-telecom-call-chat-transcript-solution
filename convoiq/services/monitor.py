"""Monitor service: streaming outage detection + drift reports.

Single-instance by design (max_instance_count=1 in Terraform): per-tower sliding windows live in memory and are rebuilt
from the warehouse on cold start.  Scale-out would shard by tower id via Pub/Sub ordering keys (docs/adr/005-monitor-single-instance.md).
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import numpy as np
from fastapi import Depends

from convoiq.anomaly import OutageDetector
from convoiq.config import Settings, get_settings
from convoiq.contracts import (ActionRequest, ActionType, EnrichedInteraction, EventEnvelope, Severity, TOPIC_ACTIONS_PROPOSED, TOPIC_ENRICHED,
                               TOPIC_INCIDENTS)
from convoiq.logging import get_logger
from convoiq.monitor import DriftMonitor
from convoiq.monitor.drift import new_report_id
from convoiq.platform.auth import Principal, require
from convoiq.platform.bus import PermanentError
from convoiq.platform.metrics import REGISTRY
from convoiq.platform.service import add_push_consumer, create_app
from convoiq.platform.warehouse import Warehouse, make_warehouse
from convoiq.services.common import Service, make_event

log = get_logger("convoiq.monitor")
_inc = REGISTRY.counter("convoiq_incidents_opened_total", "network incidents opened", [])
_open = REGISTRY.gauge("convoiq_incidents_open", "currently open incidents", [])
_drift = REGISTRY.gauge("convoiq_drift_metric", "latest drift metric value", ["metric"])
RESERVOIR = 600


def build(settings: Settings | None = None, *, bus=None, store=None, warehouse: Warehouse | None = None,
          detector: OutageDetector | None = None, drift: DriftMonitor | None = None) -> Service:
    s = settings or get_settings()
    app = create_app("monitor", "Outage detection and drift monitoring", s, bus=bus, store=store)
    wh = warehouse or make_warehouse(s)
    det = detector or OutageDetector()
    state = {"drift": drift, "emb": [], "seen": 0, "rng": random.Random(7)}
    app.state.detector = det

    async def get_drift() -> DriftMonitor | None:
        if state["drift"] is None:
            try:
                state["drift"] = DriftMonitor.from_file(settings=s)
            except Exception as e:
                log.error("drift reference unavailable", error=str(e))
        return state["drift"]

    async def rebuild() -> int:
        """Cold start: replay the last 2h of network complaints into the detector."""
        rows = await wh.query(
            f"SELECT interaction_id, cell_tower_id, region, issue_type, ingested_at FROM {wh.t('interactions_fact')} "
            f"WHERE ingested_at >= {wh.ts('since')} AND cell_tower_id IS NOT NULL ORDER BY ingested_at",
            {"since": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()})
        for r in rows:
            ts = r["ingested_at"] if isinstance(r["ingested_at"], datetime) else datetime.fromisoformat(str(r["ingested_at"]))
            det.observe(r["cell_tower_id"], ts, r["issue_type"], r["interaction_id"], r["region"])
        return len(rows)

    async def startup(_app) -> None:
        try:
            n = await rebuild()
            log.info("detector rebuilt", events=n)
        except Exception as e:      # first boot: tables may not exist yet
            log.warning("detector rebuild skipped", error=str(e))
    state["rebuilt"] = False

    async def publish_incident(inc) -> None:
        _inc.inc()
        bus_ = app.state.bus
        row = inc.as_row()
        row["detected_at"] = inc.opened_at.isoformat()
        row["window_start"], row["window_end"] = inc.window_start.isoformat(), inc.last_event_at.isoformat()
        await bus_.publish(TOPIC_INCIDENTS, make_event("incident.opened", row, "monitor", ordering_key=inc.tower_id))
        act = ActionRequest.build(inc.incident_id, ActionType.OSS_TICKET, {"tower_id": inc.tower_id, "n_complaints": inc.count,
                                                                         "expected_per_hour": round(inc.expected_rate, 2), "p_value": inc.p_value,
                                                                         "ctx": {"identity_verified": True}},
                                  rationale=f"{inc.count} network complaints in 60 min vs {inc.expected_rate:.1f}/h baseline (p={inc.p_value:.1e})",
                                  evidence=[f"incident:{inc.incident_id}"], risk=Severity.MEDIUM)
        await bus_.publish(TOPIC_ACTIONS_PROPOSED, make_event("action.proposed", act.model_dump(mode="json"), "monitor", ordering_key=inc.tower_id))

    async def on_enriched(ev: EventEnvelope) -> None:
        try:
            e = EnrichedInteraction.model_validate(ev.payload)
        except Exception as ex:
            raise PermanentError(f"invalid enriched: {type(ex).__name__}") from ex
        if not state["rebuilt"]:
            state["rebuilt"] = True
            await startup(app)
        ts = e.meta.started_at or ev.occurred_at
        inc = det.observe(e.meta.cell_tower_id, ts, e.issue.issue_type, e.interaction_id, e.meta.region)
        _open.set(len(det.open_incidents()))
        if inc:
            await publish_incident(inc)
        # reservoir sample of embeddings for MMD drift (uniform over the stream)
        state["seen"] += 1
        if e.embedding:
            if len(state["emb"]) < RESERVOIR:
                state["emb"].append(e.embedding)
            else:
                j = state["rng"].randint(0, state["seen"] - 1)
                if j < RESERVOIR:
                    state["emb"][j] = e.embedding

    handler = add_push_consumer(app, "/pubsub/enriched", on_enriched)

    @app.post("/v1/sweep")
    async def sweep(_: Principal = Depends(require("service"))):
        closed = det.sweep(datetime.now(timezone.utc))
        _open.set(len(det.open_incidents()))
        return {"closed": [i.incident_id for i in closed], "open": [i.incident_id for i in det.open_incidents()]}

    @app.get("/v1/incidents")
    async def incidents(_: Principal = Depends(require("analyst"))):
        return [i.as_row() for i in det.open_incidents()]

    @app.post("/v1/drift/run")
    async def run_drift(hours: int = 24, tenant: str = "default", _: Principal = Depends(require("service"))):
        dm = await get_drift()
        if dm is None:
            return {"status": "no_reference"}
        w1 = datetime.now(timezone.utc)
        w0 = w1 - timedelta(hours=hours)
        rows = await wh.query(
            f"SELECT issue_type, issue_confidence, sentiment_mean, n_turns, injection_score FROM {wh.t('interactions_fact')} "
            f"WHERE ingested_at >= {wh.ts('since')} AND tenant_id = {wh.p('tenant')}", {"since": w0.isoformat(), "tenant": tenant})
        emb = np.asarray(state["emb"], float) if state["emb"] else None
        metrics = dm.evaluate(rows, emb, (w0, w1))
        rid = new_report_id()
        await wh.insert("drift_reports", [m.row(tenant, rid, w0, w1) for m in metrics])
        for m in metrics:
            _drift.set(m.value, metric=m.metric)
        rec = dm.recommendation(metrics)
        log.info("drift evaluated", recommendation=rec, n_interactions=len(rows), alerts=[m.metric for m in metrics if m.status == "ALERT"])
        await app.state.store.put("drift", "latest", {"report_id": rid, "recommendation": rec, "metrics": [m.__dict__ for m in metrics],
                                                      "at": w1.isoformat()})
        return {"report_id": rid, "recommendation": rec, "metrics": [m.__dict__ for m in metrics]}

    @app.get("/v1/drift/latest")
    async def latest(_: Principal = Depends(require("analyst"))):
        return await app.state.store.get("drift", "latest") or {"status": "none"}

    return Service("monitor", app, {TOPIC_ENRICHED: handler}, state={"detector": det, "state": state})


def create():
    return build().app

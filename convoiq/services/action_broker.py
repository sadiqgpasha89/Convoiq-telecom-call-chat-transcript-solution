"""Action broker: the only service allowed to cause side effects.  Orchestrator proposes, broker disposes.

For each proposed action: idempotency -> deterministic policy (re-evaluated here, never trusting the proposer) ->
auto-execute | park for human approval | block -> connector call (retry, circuit breaker) -> DEFERRED on outage ->
append-only audit row + result event.  A global kill switch freezes all automation instantly.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from convoiq.config import Settings, get_settings
from convoiq.connectors import ConnectorRouter, make_router
from convoiq.contracts import (ActionRequest, ActionResult, ActionStatus, ActionType, ApprovalDecision, EventEnvelope,
                               TOPIC_ACTIONS_PROPOSED, TOPIC_ACTIONS_RESULT)
from convoiq.guardrails.policy import PolicyContext, evaluate
from convoiq.logging import get_logger
from convoiq.platform.auth import Principal, require
from convoiq.platform.bus import PermanentError
from convoiq.platform.metrics import REGISTRY
from convoiq.platform.service import add_push_consumer, create_app
from convoiq.platform.warehouse import Warehouse, make_warehouse
from convoiq.services.common import Service, make_event

log = get_logger("convoiq.broker")
_act = REGISTRY.counter("convoiq_actions_total", "actions by outcome", ["type", "status"])
_usd = REGISTRY.counter("convoiq_credit_executed_usd_total", "credits executed", [])

ROLLING_DAYS = 30
MAX_ATTEMPTS = 5


class KillSwitch(BaseModel):
    enabled: bool
    reason: str = ""


def build(settings: Settings | None = None, *, bus=None, store=None, warehouse: Warehouse | None = None,
          router: ConnectorRouter | None = None) -> Service:
    s = settings or get_settings()
    app = create_app("action-broker", "Policy-enforced execution of AI-proposed actions", s, bus=bus, store=store)
    wh = warehouse or make_warehouse(s)
    router = router or make_router(s)
    st = app.state.store

    async def kill_switch() -> bool:
        d = await st.get("flags", "kill_switch")
        return s.kill_switch or bool(d and d.get("enabled"))

    async def credits_30d(pid: str | None) -> float:
        if not pid:
            return 0.0
        led = await st.get("credit_ledger", pid) or {"entries": []}
        cutoff = (datetime.now(timezone.utc) - timedelta(days=ROLLING_DAYS)).isoformat()
        return sum(e["amount"] for e in led["entries"] if e["ts"] >= cutoff)

    async def add_credit(pid: str | None, amount: float) -> None:
        if not pid:
            return
        led = await st.get("credit_ledger", pid) or {"entries": []}
        cutoff = (datetime.now(timezone.utc) - timedelta(days=ROLLING_DAYS)).isoformat()
        led["entries"] = [e for e in led["entries"] if e["ts"] >= cutoff] + [{"ts": datetime.now(timezone.utc).isoformat(), "amount": amount}]
        await st.put("credit_ledger", pid, led)

    async def audit(a: ActionRequest, status: ActionStatus, msg: str = "", decisions: list[str] | None = None, ref: str | None = None,
                    approver: str | None = None, attempts: int = 0, target: str | None = None) -> ActionResult:
        res = ActionResult(action_id=a.action_id, interaction_id=a.interaction_id, type=a.type, status=status, target_system=target,
                           external_ref=ref, message=msg[:500], policy_decisions=decisions or [], approver=approver, attempts=attempts,
                           executed_at=datetime.now(timezone.utc) if status == ActionStatus.EXECUTED else None)
        await wh.insert("actions_audit", [{"action_id": a.action_id, "interaction_id": a.interaction_id, "tenant_id": a.tenant_id,
                                           "ts": datetime.now(timezone.utc), "type": a.type.value, "status": status.value,
                                           "params_json": json.dumps({k: v for k, v in a.params.items() if k != "note"}, default=str)[:4000],
                                           "amount_usd": a.amount_usd, "risk": a.risk.value, "policy_decisions": "; ".join(decisions or []),
                                           "approver": approver, "external_ref": ref, "message": msg[:500], "attempts": attempts}])
        _act.inc(type=a.type.value, status=status.value)
        await app.state.bus.publish(TOPIC_ACTIONS_RESULT, make_event("action.result", res.model_dump(mode="json"), "action-broker",
                                                                     ordering_key=a.customer_pseudo_id))
        return res

    async def execute(a: ActionRequest, decisions: list[str], approver: str | None = None) -> ActionResult:
        conn = router.for_action(a.type)
        rec = await st.get("actions", a.action_id) or {}
        attempts = int(rec.get("attempts", 0)) + 1
        if conn is None:
            await st.put("actions", a.action_id, {**rec, "request": a.model_dump(mode="json"), "status": ActionStatus.FAILED.value, "attempts": attempts})
            return await audit(a, ActionStatus.FAILED, "no connector configured for action type", decisions, approver=approver, attempts=attempts)
        r = await conn.execute(a)
        if r.ok:
            status = ActionStatus.EXECUTED
            if a.type == ActionType.BSS_CREDIT and a.amount_usd:
                await add_credit(a.customer_pseudo_id, float(a.amount_usd))
                _usd.inc(float(a.amount_usd))
            if a.type == ActionType.OSS_TICKET and a.params.get("tower_id"):
                await st.put("open_tickets", str(a.params["tower_id"]), {"ref": r.external_ref, "action_id": a.action_id}, ttl=6 * 3600)
        elif r.retryable and attempts < MAX_ATTEMPTS:
            status = ActionStatus.DEFERRED
        else:
            status = ActionStatus.FAILED
        await st.put("actions", a.action_id, {**rec, "request": a.model_dump(mode="json"), "status": status.value, "attempts": attempts,
                                              "decisions": decisions, "external_ref": r.external_ref, "approver": approver})
        return await audit(a, status, r.message, decisions, r.external_ref, approver, attempts, r.target or conn.name)

    async def handle(a: ActionRequest) -> ActionResult:
        first = await st.put_if_absent("actions", a.action_id, {"request": a.model_dump(mode="json"), "status": ActionStatus.PROPOSED.value, "attempts": 0})
        if not first:
            existing = await st.get("actions", a.action_id) or {}
            return ActionResult(action_id=a.action_id, interaction_id=a.interaction_id, type=a.type, status=ActionStatus.DUPLICATE,
                                message=f"already {existing.get('status')}")
        c = a.params.get("ctx") or {}
        tower = a.params.get("tower_id")
        pctx = PolicyContext(identity_verified=c.get("identity_verified"), injection_detected=bool(c.get("injection_detected")),
                             fraud_suspected=bool(c.get("fraud_suspected")), credits_last_30d_usd=await credits_30d(a.customer_pseudo_id),
                             existing_open_ticket_for_tower=bool(tower and await st.get("open_tickets", str(tower))),
                             kill_switch=await kill_switch(), dry_run=s.connector_mode == "dryrun")
        d = evaluate(a, pctx, s)
        if not d.allowed:
            await st.put("actions", a.action_id, {"request": a.model_dump(mode="json"), "status": ActionStatus.BLOCKED_BY_POLICY.value, "decisions": d.reasons})
            return await audit(a, ActionStatus.BLOCKED_BY_POLICY, "; ".join(d.reasons), d.reasons)
        if d.requires_approval:
            await st.put("actions", a.action_id, {"request": a.model_dump(mode="json"), "status": ActionStatus.PENDING_APPROVAL.value,
                                                  "decisions": d.reasons, "attempts": 0})
            return await audit(a, ActionStatus.PENDING_APPROVAL, "awaiting human approval", d.reasons)
        return await execute(a, d.reasons)

    async def on_proposed(ev: EventEnvelope) -> None:
        try:
            a = ActionRequest.model_validate(ev.payload)
        except Exception as e:
            raise PermanentError(f"invalid action: {type(e).__name__}") from e
        await handle(a)

    handler = add_push_consumer(app, "/pubsub/actions", on_proposed)

    # ---- human approval API ---------------------------------------------------------------------------------------
    @app.get("/v1/approvals")
    async def pending(_: Principal = Depends(require("approver", "service"))):
        docs = await st.list("actions", limit=1000)
        return [{"action_id": d["request"]["action_id"], "type": d["request"]["type"], "amount_usd": d["request"].get("amount_usd"),
                 "rationale": d["request"].get("rationale"), "evidence": d["request"].get("evidence"), "risk": d["request"].get("risk"),
                 "reasons": d.get("decisions")} for d in docs if d.get("status") == ActionStatus.PENDING_APPROVAL.value]

    @app.post("/v1/approvals/{action_id}", response_model=ActionResult)
    async def decide(action_id: str, body: ApprovalDecision, p: Principal = Depends(require("approver", "service"))):
        rec = await st.get("actions", action_id)
        if rec is None or rec.get("status") != ActionStatus.PENDING_APPROVAL.value:
            raise HTTPException(404, "no pending action with that id")
        a = ActionRequest.model_validate(rec["request"])
        if body.approved:
            # re-check hard limits at approval time (state may have changed since proposal)
            c = a.params.get("ctx") or {}
            d = evaluate(a, PolicyContext(identity_verified=c.get("identity_verified"), credits_last_30d_usd=await credits_30d(a.customer_pseudo_id),
                                          kill_switch=await kill_switch()), s)
            if not d.allowed:
                await st.put("actions", action_id, {**rec, "status": ActionStatus.BLOCKED_BY_POLICY.value, "decisions": d.reasons})
                return await audit(a, ActionStatus.BLOCKED_BY_POLICY, "; ".join(d.reasons), d.reasons, approver=p.subject)
            return await execute(a, rec.get("decisions", []) + [f"approved by {p.subject}: {body.comment}"], approver=p.subject)
        await st.put("actions", action_id, {**rec, "status": ActionStatus.REJECTED.value})
        return await audit(a, ActionStatus.REJECTED, body.comment or "rejected", rec.get("decisions"), approver=p.subject)

    @app.post("/v1/reconcile")
    async def reconcile(_: Principal = Depends(require("service", "admin"))):
        """Cloud Scheduler: retry DEFERRED actions (connector outages) with the original idempotency key."""
        n = ok = 0
        for d in await st.list("actions", limit=2000):
            if d.get("status") == ActionStatus.DEFERRED.value:
                n += 1
                res = await execute(ActionRequest.model_validate(d["request"]), d.get("decisions", []))
                ok += res.status == ActionStatus.EXECUTED
        return {"retried": n, "executed": ok}

    @app.post("/admin/kill-switch")
    async def set_kill(k: KillSwitch, p: Principal = Depends(require("admin"))):
        await st.put("flags", "kill_switch", {"enabled": k.enabled, "reason": k.reason, "by": p.subject, "ts": datetime.now(timezone.utc).isoformat()})
        log.warning("kill switch changed", enabled=k.enabled, by=p.subject, reason=k.reason)
        return {"enabled": k.enabled}

    app.state.handle = handle
    return Service("action-broker", app, {TOPIC_ACTIONS_PROPOSED: handler}, state={"handle": handle, "warehouse": wh})


def create():
    return build().app

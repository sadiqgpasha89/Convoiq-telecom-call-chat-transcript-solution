"""Action connectors: the only code that writes to enterprise systems (CRM / OSS / BSS / fraud tooling).

Contract: ``execute`` must be *idempotent on action_id* (we pass it as the Idempotency-Key) and returns a typed
result; transient failures are ``retryable=True`` so the broker can DEFER and reconcile instead of losing the action.
The real-system connectors implement public REST shapes (Salesforce sObjects, ServiceNow Table API) but have NOT
been exercised against live tenants from this repository - treat them as integration scaffolds (docs/INTEGRATIONS.md).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

from convoiq.config import Settings, get_settings
from convoiq.contracts import ActionRequest, ActionType
from convoiq.platform.resilience import CircuitBreaker, retry


@dataclass
class ConnectorResult:
    ok: bool
    external_ref: str | None = None
    message: str = ""
    retryable: bool = False
    target: str = ""


class Connector(Protocol):
    name: str
    def supports(self, t: ActionType) -> bool: ...
    async def execute(self, action: ActionRequest) -> ConnectorResult: ...


class DryRunConnector:
    name = "dryrun"

    def supports(self, t: ActionType) -> bool:
        return True

    async def execute(self, action: ActionRequest) -> ConnectorResult:
        return ConnectorResult(True, f"dryrun:{action.action_id[:12]}", "dry-run: no external side effects", target="dryrun")


class _RestBase:
    name = "rest"
    types: set[ActionType] = set()

    def __init__(self, base_url: str, token: str = "", settings: Settings | None = None, client: httpx.AsyncClient | None = None):
        s = settings or get_settings()
        self.base, self.token = base_url.rstrip("/"), token
        self.client = client or httpx.AsyncClient(timeout=8.0)
        self.breaker = CircuitBreaker(self.name, s.circuit_fail_threshold, s.circuit_reset_s)

    def supports(self, t: ActionType) -> bool:
        return t in self.types

    def _headers(self, action: ActionRequest) -> dict[str, str]:
        h = {"Idempotency-Key": action.action_id, "Content-Type": "application/json", "X-Interaction-Id": action.interaction_id}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def _post(self, path: str, body: dict, action: ActionRequest, ref_key: str = "id") -> ConnectorResult:
        async def once() -> httpx.Response:
            r = await self.client.post(self.base + path, json=body, headers=self._headers(action))
            if r.status_code >= 500 or r.status_code == 429:
                raise httpx.HTTPStatusError("transient", request=r.request, response=r)
            return r

        try:
            r = await self.breaker.call(lambda: retry(once, attempts=3, base=0.2, name=self.name))
        except Exception as e:
            return ConnectorResult(False, None, f"{type(e).__name__}", retryable=True, target=self.name)
        if r.status_code >= 400:
            return ConnectorResult(False, None, f"HTTP {r.status_code}", retryable=False, target=self.name)
        data = r.json() if r.content else {}
        return ConnectorResult(True, str(data.get(ref_key) or data.get("sys_id") or data.get("ref") or ""), "ok", target=self.name)


class SimulatedConnector(_RestBase):
    """Talks to the bundled telecom-sim service (also the contract the real connectors are tested against)."""
    name = "telecom-sim"
    types = set(ActionType)

    async def execute(self, action: ActionRequest) -> ConnectorResult:
        pid = action.customer_pseudo_id or "unknown"
        t = action.type
        p = {k: v for k, v in action.params.items() if k != "ctx"}
        if t == ActionType.BSS_CREDIT:
            return await self._post(f"/bss/customers/{pid}/credits", {"amount_usd": action.amount_usd, **p}, action, "credit_id")
        if t == ActionType.RETENTION_OFFER:
            return await self._post(f"/bss/customers/{pid}/offers", p, action, "offer_id")
        if t == ActionType.OSS_TICKET:
            return await self._post("/oss/tickets", p, action, "ticket_id")
        if t == ActionType.CRM_NOTE:
            return await self._post(f"/crm/cases/{action.interaction_id}/notes", {"note": p.get("note", ""), "meta": p}, action, "note_id")
        if t in (ActionType.ESCALATE_FRAUD, ActionType.ESCALATE_HUMAN):
            return await self._post("/escalations", {"kind": t.value, "interaction_id": action.interaction_id, **p}, action, "escalation_id")
        return await self._post("/callbacks", {"interaction_id": action.interaction_id, **p}, action, "callback_id")


class SalesforceConnector(_RestBase):
    """CRM_NOTE -> Salesforce Case comment (REST: POST /services/data/vXX.0/sobjects/CaseComment)."""
    name = "salesforce"
    types = {ActionType.CRM_NOTE, ActionType.ESCALATE_HUMAN, ActionType.CALLBACK_SCHEDULE}

    async def execute(self, action: ActionRequest) -> ConnectorResult:
        body = {"ParentId": action.interaction_id, "CommentBody": str(action.params.get("note") or action.rationale)[:3900], "IsPublished": False}
        return await self._post("/services/data/v60.0/sobjects/CaseComment", body, action)


class ServiceNowConnector(_RestBase):
    """OSS_TICKET / ESCALATE_FRAUD -> ServiceNow Table API incident."""
    name = "servicenow"
    types = {ActionType.OSS_TICKET, ActionType.ESCALATE_FRAUD}

    async def execute(self, action: ActionRequest) -> ConnectorResult:
        p = action.params
        body = {"short_description": f"[ConvoIQ] {action.type.value} {p.get('tower_id', '')}".strip(),
                "description": action.rationale, "category": "network" if action.type == ActionType.OSS_TICKET else "security",
                "urgency": "2", "correlation_id": action.action_id}
        return await self._post("/api/now/table/incident", body, action, "sys_id")


class BillingConnector(_RestBase):
    """BSS_CREDIT / RETENTION_OFFER -> billing platform adjustments API (shape is tenant-specific; adapt mapping here)."""
    name = "billing"
    types = {ActionType.BSS_CREDIT, ActionType.RETENTION_OFFER}

    async def execute(self, action: ActionRequest) -> ConnectorResult:
        pid = action.customer_pseudo_id
        if action.type == ActionType.BSS_CREDIT:
            return await self._post(f"/v1/accounts/{pid}/adjustments", {"type": "CREDIT", "amount": action.amount_usd, "reason": action.params.get("reason")}, action)
        return await self._post(f"/v1/accounts/{pid}/promotions", {"discount_pct": action.params.get("discount_pct"), "months": action.params.get("months")}, action)


class ConnectorRouter:
    def __init__(self, connectors: list[Connector]):
        self.connectors = connectors

    def for_action(self, t: ActionType) -> Connector | None:
        return next((c for c in self.connectors if c.supports(t)), None)


def make_router(settings: Settings | None = None, client: httpx.AsyncClient | None = None) -> ConnectorRouter:
    s = settings or get_settings()
    if s.connector_mode == "dryrun":
        return ConnectorRouter([DryRunConnector()])
    if s.connector_mode == "real":
        cs: list[Connector] = []
        if s.servicenow_url: cs.append(ServiceNowConnector(s.servicenow_url, s.servicenow_token, s, client))
        if s.billing_url: cs.append(BillingConnector(s.billing_url, s.billing_token, s, client))
        if s.salesforce_url: cs.append(SalesforceConnector(s.salesforce_url, s.salesforce_token, s, client))
        return ConnectorRouter(cs)
    return ConnectorRouter([SimulatedConnector(s.telecom_sim_url, "", s, client)])

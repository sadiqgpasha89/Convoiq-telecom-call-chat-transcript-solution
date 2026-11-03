"""Action contracts - the only way an AI decision can touch an enterprise system."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from pydantic import Field

from .enums import ActionStatus, ActionType, Severity
from .interaction import Contract


def action_key(interaction_id: str, action_type: str, params: dict[str, Any]) -> str:
    """Deterministic idempotency key: the same decision never executes twice (at-least-once bus)."""
    blob = json.dumps({"i": interaction_id, "t": action_type, "p": params}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


class ActionRequest(Contract):
    action_id: str
    interaction_id: str
    tenant_id: str = "default"
    type: ActionType
    params: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    evidence: list[str] = Field(default_factory=list)       # e.g. ["turn:4", "kb:tech_support_manual#12"]
    risk: Severity = Severity.LOW
    amount_usd: float | None = None
    customer_pseudo_id: str | None = None
    created_by: str = "action_planner"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def build(cls, interaction_id: str, type: ActionType, params: dict[str, Any], **kw: Any) -> "ActionRequest":
        return cls(action_id=action_key(interaction_id, type.value, params), interaction_id=interaction_id,
                   type=type, params=params, **kw)


class ActionResult(Contract):
    action_id: str
    interaction_id: str
    type: ActionType
    status: ActionStatus
    target_system: str | None = None
    external_ref: str | None = None
    message: str = ""
    policy_decisions: list[str] = Field(default_factory=list)
    approver: str | None = None
    executed_at: datetime | None = None
    attempts: int = 0


class ApprovalDecision(Contract):
    action_id: str
    approved: bool
    approver: str
    comment: str = ""

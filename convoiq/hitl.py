"""Human-in-the-loop review queue: quarantines, guard blocks, low-confidence diagnoses and a random QA sample.

Verdicts feed (a) the label-based drift/perf monitors and (b) the retraining set (docs/MLOPS.md).
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from convoiq.contracts import Analysis
from convoiq.platform.store import Store

COLL = "hitl"


def sampled(interaction_id: str, rate: float) -> bool:
    """Deterministic sampling: the same interaction is always (not) sampled, across replicas and retries."""
    h = int(hashlib.sha256(interaction_id.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return h < rate


def review_reasons(a: Analysis, rate: float) -> list[str]:
    r = []
    if a.guardrail.blocked:
        r.append("guardrail_blocked")
    if a.degraded:
        r.append("degraded_analysis")
    if a.diagnosis is not None and a.diagnosis.primary_tag is None and a.issue_type in ("NO_SERVICE", "MOBILE_DATA", "MMS"):
        r.append("no_confident_root_cause")
    if a.faithfulness is not None and a.faithfulness < 0.8:
        r.append("low_faithfulness")
    if a.compliance and any(v.severity.value in ("HIGH", "CRITICAL") and not v.systemic for v in a.compliance.violations):
        r.append("high_severity_violation")
    if not r and sampled(a.interaction_id, rate):
        r.append("random_qa_sample")
    return r


class HitlQueue:
    def __init__(self, store: Store):
        self.store = store

    async def enqueue(self, a: Analysis, reasons: list[str]) -> str:
        rid = "rev-" + hashlib.sha256(a.interaction_id.encode()).hexdigest()[:14]
        doc = {"review_id": rid, "interaction_id": a.interaction_id, "tenant_id": a.tenant_id, "created_at": datetime.now(timezone.utc).isoformat(),
               "reason": ",".join(reasons), "status": "PENDING", "reviewer": None, "verdict": None,
               "snapshot": {"summary": a.summary, "issue_type": a.issue_type, "root_cause": a.diagnosis.primary_tag if a.diagnosis else None,
                            "violations": [v.rule_id for v in (a.compliance.violations if a.compliance else [])],
                            "actions": [x.type.value for x in a.proposed_actions]}}
        await self.store.put_if_absent(COLL, rid, doc)
        return rid

    async def pending(self, limit: int = 50) -> list[dict]:
        return [d for d in await self.store.list(COLL, limit=500) if d.get("status") == "PENDING"][:limit]

    async def resolve(self, review_id: str, reviewer: str, verdict: dict[str, Any]) -> dict | None:
        doc = await self.store.get(COLL, review_id)
        if doc is None or doc["status"] != "PENDING":
            return None
        doc.update(status="REVIEWED", reviewer=reviewer, verdict=verdict, reviewed_at=datetime.now(timezone.utc).isoformat())
        await self.store.put(COLL, review_id, doc)
        return doc

    async def stats(self) -> dict[str, Any]:
        docs = await self.store.list(COLL, limit=2000)
        rev = [d for d in docs if d["status"] == "REVIEWED"]
        agree = [d for d in rev if d["verdict"] and "root_cause_correct" in d["verdict"]]
        return {"pending": sum(d["status"] == "PENDING" for d in docs), "reviewed": len(rev),
                "root_cause_agreement": (sum(bool(d["verdict"]["root_cause_correct"]) for d in agree) / len(agree)) if agree else None}

"""Output of the enrichment service (NLP + classical ML)."""
from __future__ import annotations

from typing import Any

from pydantic import Field

from .enums import Channel, Tier
from .interaction import Contract, InteractionMeta, Turn


class SentimentTrace(Contract):
    per_turn: list[float] = Field(default_factory=list)       # customer turns, -1..1
    turn_ids: list[int] = Field(default_factory=list)
    mean: float = 0.0
    minimum: float = 0.0
    delta: float = 0.0                                          # last third - first third
    trend: str = "flat"                                         # improving | worsening | flat
    frustration_index: float = Field(default=0.0, ge=0, le=1)
    sarcasm_score: float = Field(default=0.0, ge=0, le=1)
    escalation_demands: int = 0


class IssuePrediction(Contract):
    issue_type: str
    confidence: float = Field(ge=0, le=1)
    probs: dict[str, float] = Field(default_factory=dict)


class RootCauseHypothesis(Contract):
    tag: str
    prob: float = Field(ge=0, le=1)


class ChurnSignals(Contract):
    competitor_mentions: list[str] = Field(default_factory=list)
    cancel_intent: bool = False
    port_out_request: bool = False
    price_complaint: bool = False
    tenure_months: int | None = None
    score: float = Field(default=0.0, ge=0, le=1)               # conversation-only churn propensity
    evidence_turns: list[int] = Field(default_factory=list)


class EnrichedInteraction(Contract):
    schema_version: str = "1.0"
    interaction_id: str
    tenant_id: str = "default"
    channel: Channel
    customer_pseudo_id: str | None = None
    turns: list[Turn]
    meta: InteractionMeta = Field(default_factory=InteractionMeta)
    language: dict[str, Any] = Field(default_factory=dict)
    issue: IssuePrediction
    root_causes: list[RootCauseHypothesis] = Field(default_factory=list)
    sentiment: SentimentTrace = Field(default_factory=SentimentTrace)
    churn: ChurnSignals = Field(default_factory=ChurnSignals)
    entities: dict[str, list[str]] = Field(default_factory=dict)
    resolution_risk: float = Field(default=0.5, ge=0, le=1)     # P(unresolved) calibrated
    injection_score: float = Field(default=0.0, ge=0, le=1)
    injection_flags: list[str] = Field(default_factory=list)
    fraud_score: float = Field(default=0.0, ge=0, le=1)
    tier: Tier = Tier.TIER3_AGENTIC
    tier_reason: str = ""
    embedding: list[float] = Field(default_factory=list)
    model_versions: dict[str, str] = Field(default_factory=dict)
    latency_ms: float = 0.0

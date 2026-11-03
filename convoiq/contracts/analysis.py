"""Output of the agent orchestrator: grounded, auditable, machine-actionable."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import Field

from .actions import ActionRequest
from .enums import AnalysisMode, Severity, Tier
from .interaction import Contract


class Evidence(Contract):
    kind: str                       # turn | kb | record | tool | model
    ref: str                        # turn:5 | kb:main_policy#2 | bss:bill:B1002 | model:rootcause@1.0
    quote: str | None = None


class Claim(Contract):
    text: str
    evidence: list[Evidence] = Field(default_factory=list)
    attributed: bool = False        # set by the grounding verifier


class Violation(Contract):
    rule_id: str
    title: str
    severity: Severity
    turn_ids: list[int] = Field(default_factory=list)
    explanation: str = ""
    systemic: bool = False          # platform/process fault rather than CSR misconduct (non-punitive eval)
    evidence: list[Evidence] = Field(default_factory=list)


class ComplianceResult(Contract):
    score: float = Field(ge=0, le=1)
    violations: list[Violation] = Field(default_factory=list)
    rules_checked: list[str] = Field(default_factory=list)


class Hypothesis(Contract):
    tag: str
    label: str
    domain: str
    prob: float
    status: str = "plausible"       # confirmed | plausible | rejected
    evidence: list[Evidence] = Field(default_factory=list)


class Diagnosis(Contract):
    primary_tag: str | None = None
    primary_label: str | None = None
    owner_domain: str | None = None
    confidence: float = 0.0
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    recommended_fixes: list[str] = Field(default_factory=list)     # "user:toggle_airplane_mode"
    context: dict[str, Any] = Field(default_factory=dict)          # BSS/OSS facts used


class ChurnDriver(Contract):
    feature: str
    contribution: float


class ChurnAssessment(Contract):
    risk_score: float = Field(ge=0, le=1)
    band: str = "LOW"
    drivers: list[ChurnDriver] = Field(default_factory=list)
    competitor: str | None = None
    recommended_offer: dict[str, Any] | None = None
    expected_value_at_risk_usd: float | None = None


class TrajectoryQA(Contract):
    """Agent (CSR / bot) trajectory audit - computed from tool calls & text, no gold needed."""
    n_tool_calls: int = 0
    n_errors: int = 0
    redundant_calls: int = 0
    repeated_failures: int = 0
    identity_checked_first: bool | None = None
    transfers: int = 0
    policy_flags: list[str] = Field(default_factory=list)
    efficiency: float | None = None


class TraceStep(Contract):
    node: str
    status: str = "ok"
    duration_ms: float = 0.0
    detail: str = ""


class GuardrailReport(Contract):
    blocked: bool = False
    input_flags: list[str] = Field(default_factory=list)
    output_flags: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class Analysis(Contract):
    schema_version: str = "1.0"
    interaction_id: str
    tenant_id: str = "default"
    customer_pseudo_id: str | None = None
    mode: AnalysisMode
    tier: Tier
    issue_type: str
    summary: str = ""
    claims: list[Claim] = Field(default_factory=list)
    diagnosis: Diagnosis | None = None
    compliance: ComplianceResult | None = None
    churn: ChurnAssessment | None = None
    qa: TrajectoryQA | None = None
    proposed_actions: list[ActionRequest] = Field(default_factory=list)
    guardrail: GuardrailReport = Field(default_factory=GuardrailReport)
    trace: list[TraceStep] = Field(default_factory=list)
    llm: dict[str, Any] = Field(default_factory=dict)
    faithfulness: float | None = None
    degraded: bool = False
    degraded_reasons: list[str] = Field(default_factory=list)
    value_estimate: dict[str, float] = Field(default_factory=dict)
    versions: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    latency_ms: float = 0.0

"""Interaction payloads: raw (sovereign zone) -> redacted (cloud-safe)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .enums import Channel, Speaker


class Contract(BaseModel):
    """Base class: ignore unknown fields so producers can add fields without breaking consumers."""
    model_config = ConfigDict(extra="ignore", protected_namespaces=())


class ToolCall(Contract):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    requestor: str = "assistant"


class Turn(Contract):
    i: int
    role: Speaker
    text: str = ""
    start_ms: int | None = None
    end_ms: int | None = None
    asr_confidence: float | None = Field(default=None, ge=0, le=1)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    requestor: str | None = None
    is_error: bool = False

    @field_validator("text")
    @classmethod
    def _cap_len(cls, v: str) -> str:
        if len(v) > 8000:
            raise ValueError("turn text exceeds 8000 chars")
        return v


class InteractionMeta(Contract):
    customer_ref: str | None = None          # raw MSISDN / customer id - only present pre-redaction
    msisdn: str | None = None                 # raw - removed by the redactor
    cell_tower_id: str | None = None
    region: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    plan: str | None = None
    tenure_months: int | None = None
    locale: str | None = None
    termination_code: str | None = None       # NORMAL_CLEARING | SIP_BYE_NETWORK_DROP | ...
    device_model: str | None = None
    agent_id: str | None = None               # pseudonymous CSR id
    queue: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class RawInteraction(Contract):
    schema_version: str = "1.0"
    interaction_id: str = Field(min_length=3, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    tenant_id: str = "default"
    channel: Channel = Channel.CHAT_APP
    turns: list[Turn] = Field(min_length=1, max_length=500)
    meta: InteractionMeta = Field(default_factory=InteractionMeta)
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RedactionAttestation(Contract):
    """Proof (for the cloud side) that the payload went through the redaction engine."""
    engine: str = "convoiq-redactor"
    version: str = "1.0.0"
    content_sha256: str
    counts: dict[str, int] = Field(default_factory=dict)
    residual_findings: int = 0
    zone: str = "edge"                        # edge | cloud-quarantine
    signature: str | None = None              # HMAC(content_sha256 | interaction_id) when edge->cloud


class RedactedInteraction(Contract):
    schema_version: str = "1.0"
    interaction_id: str
    tenant_id: str = "default"
    channel: Channel = Channel.CHAT_APP
    customer_pseudo_id: str | None = None
    turns: list[Turn]
    meta: InteractionMeta = Field(default_factory=InteractionMeta)
    attestation: RedactionAttestation
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def customer_text(self) -> str:
        return "\n".join(t.text for t in self.turns if t.role == Speaker.CUSTOMER and t.text)

    @property
    def full_text(self) -> str:
        return "\n".join(f"{t.role.value}: {t.text}" for t in self.turns if t.text)

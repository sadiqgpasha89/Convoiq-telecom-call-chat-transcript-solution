"""Enumerations shared by every ConvoIQ service contract (schema_version 1.x)."""
from __future__ import annotations

from enum import Enum


class Channel(str, Enum):
    CHAT_APP = "CHAT_APP"
    CHAT_WHATSAPP = "CHAT_WHATSAPP"
    CHAT_SMS = "CHAT_SMS"
    VOICE = "VOICE"
    IVR = "IVR"

    @property
    def is_voice(self) -> bool:
        return self in (Channel.VOICE, Channel.IVR)


class Speaker(str, Enum):
    CUSTOMER = "customer"
    AGENT = "agent"
    BOT = "bot"
    SYSTEM = "system"
    TOOL = "tool"


class Tier(str, Enum):
    TIER2_FAST = "TIER2_FAST"            # classical ML + templated summary, no LLM
    TIER3_AGENTIC = "TIER3_AGENTIC"      # full multi-agent reasoning


class Severity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        return ["LOW", "MEDIUM", "HIGH", "CRITICAL"].index(self.value)


class ActionType(str, Enum):
    CRM_NOTE = "CRM_NOTE"                    # Salesforce case note / summary
    OSS_TICKET = "OSS_TICKET"                # ServiceNow network incident
    BSS_CREDIT = "BSS_CREDIT"                # Amdocs/Ericsson billing credit
    RETENTION_OFFER = "RETENTION_OFFER"      # proactive discount / save offer
    ESCALATE_FRAUD = "ESCALATE_FRAUD"        # lock & hand to fraud team
    ESCALATE_HUMAN = "ESCALATE_HUMAN"
    CALLBACK_SCHEDULE = "CALLBACK_SCHEDULE"


class ActionStatus(str, Enum):
    PROPOSED = "PROPOSED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    DEFERRED = "DEFERRED"                    # connector down -> async reconciliation
    BLOCKED_BY_POLICY = "BLOCKED_BY_POLICY"
    DUPLICATE = "DUPLICATE"


class AnalysisMode(str, Enum):
    FAST_PATH = "FAST_PATH"
    SWARM = "SWARM"
    SAFE_MODE = "SAFE_MODE"                  # guardrail tripped -> deterministic minimal output

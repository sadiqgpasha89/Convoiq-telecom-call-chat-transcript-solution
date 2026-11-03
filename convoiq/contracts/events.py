"""Event envelope + topic names for the Pub/Sub backbone."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import Field

from .interaction import Contract

TOPIC_RAW = "convoiq-interactions-raw"              # sovereign zone only (CMEK, 1-day retention)
TOPIC_REDACTED = "convoiq-interactions-redacted"
TOPIC_ENRICHED = "convoiq-interactions-enriched"
TOPIC_ANALYZED = "convoiq-interactions-analyzed"
TOPIC_ACTIONS_PROPOSED = "convoiq-actions-proposed"
TOPIC_ACTIONS_RESULT = "convoiq-actions-result"
TOPIC_INCIDENTS = "convoiq-incidents"
TOPIC_DLQ = "convoiq-dead-letter"
ALL_TOPICS = [TOPIC_RAW, TOPIC_REDACTED, TOPIC_ENRICHED, TOPIC_ANALYZED, TOPIC_ACTIONS_PROPOSED,
              TOPIC_ACTIONS_RESULT, TOPIC_INCIDENTS, TOPIC_DLQ]


class EventEnvelope(Contract):
    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    type: str                                   # e.g. interaction.redacted
    version: str = "1"
    source: str = "unknown"
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    trace_id: str | None = None
    ordering_key: str | None = None             # customer_pseudo_id -> per-subscriber ordering
    payload: dict[str, Any]

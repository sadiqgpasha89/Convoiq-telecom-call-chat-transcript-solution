"""Event topology - the single source of truth for Pub/Sub wiring.

Consumed by: ``scripts/gen_infra.py`` (-> infra/terraform/generated/*.json), the docker-compose emulator bootstrap, and
``tests/v2/test_topology.py`` which proves that every push path exists on the service it targets and that the in-process
runner subscribes exactly the same (topic, service) pairs.  Service ids are Cloud Run service names (hyphenated).
"""
from __future__ import annotations

from convoiq.contracts.events import (TOPIC_ACTIONS_PROPOSED, TOPIC_ACTIONS_RESULT, TOPIC_ANALYZED, TOPIC_DLQ, TOPIC_ENRICHED,
                                      TOPIC_INCIDENTS, TOPIC_RAW, TOPIC_REDACTED)

TOPICS: list[str] = [TOPIC_RAW, TOPIC_REDACTED, TOPIC_ENRICHED, TOPIC_ANALYZED, TOPIC_ACTIONS_PROPOSED, TOPIC_ACTIONS_RESULT, TOPIC_INCIDENTS, TOPIC_DLQ]

# push subscriptions: one per (topic, consuming service). ack_deadline reflects the slowest handler on that path.
SUBSCRIPTIONS: list[dict] = [
    {"name": "redaction-raw", "topic": TOPIC_RAW, "service": "redaction", "path": "/pubsub/raw", "ack_deadline_s": 60},
    {"name": "enrichment-redacted", "topic": TOPIC_REDACTED, "service": "enrichment", "path": "/pubsub/redacted", "ack_deadline_s": 60},
    {"name": "orchestrator-enriched", "topic": TOPIC_ENRICHED, "service": "orchestrator", "path": "/pubsub/enriched", "ack_deadline_s": 120},
    {"name": "analytics-enriched", "topic": TOPIC_ENRICHED, "service": "analytics", "path": "/pubsub/enriched", "ack_deadline_s": 60},
    {"name": "monitor-enriched", "topic": TOPIC_ENRICHED, "service": "monitor", "path": "/pubsub/enriched", "ack_deadline_s": 30},
    {"name": "analytics-analyzed", "topic": TOPIC_ANALYZED, "service": "analytics", "path": "/pubsub/analyzed", "ack_deadline_s": 60},
    {"name": "broker-actions", "topic": TOPIC_ACTIONS_PROPOSED, "service": "action-broker", "path": "/pubsub/actions", "ack_deadline_s": 120},
    {"name": "analytics-actions-result", "topic": TOPIC_ACTIONS_RESULT, "service": "analytics", "path": "/pubsub/actions-result", "ack_deadline_s": 60},
    {"name": "analytics-incidents", "topic": TOPIC_INCIDENTS, "service": "analytics", "path": "/pubsub/incidents", "ack_deadline_s": 60},
]

# who may publish to which topic (drives per-topic IAM; every service may also publish dead letters)
PUBLISHERS: dict[str, list[str]] = {
    TOPIC_RAW: ["gateway"],
    TOPIC_REDACTED: ["gateway", "redaction"],
    TOPIC_ENRICHED: ["enrichment"],
    TOPIC_ANALYZED: ["orchestrator"],
    TOPIC_ACTIONS_PROPOSED: ["orchestrator", "monitor"],
    TOPIC_ACTIONS_RESULT: ["action-broker"],
    TOPIC_INCIDENTS: ["monitor"],
    TOPIC_DLQ: ["gateway", "redaction", "enrichment", "orchestrator", "action-broker", "analytics", "monitor"],
}

# Cloud Run service id -> python module under convoiq.services
SERVICE_MODULES: dict[str, str] = {
    "gateway": "gateway", "redaction": "redaction", "enrichment": "enrichment", "orchestrator": "orchestrator",
    "action-broker": "action_broker", "analytics": "analytics", "monitor": "monitor", "telecom-sim": "telecom_sim",
}

MAX_DELIVERY_ATTEMPTS = 5

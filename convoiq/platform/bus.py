"""Event bus abstraction: in-memory (tests / single-process dev) and Google Pub/Sub.

Delivery semantics are **at-least-once**; every consumer is idempotent (see ``idempotency``).  The
Pub/Sub push endpoint contract is implemented once in ``service.add_push_consumer``.
"""
from __future__ import annotations

import asyncio
import base64
import json
from collections import defaultdict
from typing import Awaitable, Callable, Protocol

from convoiq.config import Settings, get_settings
from convoiq.contracts import EventEnvelope
from convoiq.contracts.events import TOPIC_DLQ
from convoiq.logging import get_logger
from convoiq.platform.metrics import REGISTRY

log = get_logger("convoiq.bus")
_pub = REGISTRY.counter("convoiq_events_published_total", "events published", ["topic"])
_dlq = REGISTRY.counter("convoiq_events_dead_lettered_total", "events sent to DLQ", ["topic"])

Handler = Callable[[EventEnvelope], Awaitable[None]]


class PermanentError(Exception):
    """Poison message / validation failure: retrying cannot help -> dead-letter and ack."""


class EventBus(Protocol):
    async def publish(self, topic: str, event: EventEnvelope) -> str: ...


class InMemoryBus:
    """Synchronous-ish bus used by tests and the single-process local runner.

    ``deliver=True`` dispatches to subscribers immediately (await chain) with the same retry/DLQ rules
    Pub/Sub applies (max_delivery_attempts), so integration tests exercise the real failure paths.
    """

    def __init__(self, deliver: bool = True, max_delivery_attempts: int = 3):
        self.subs: dict[str, list[tuple[str, Handler]]] = defaultdict(list)
        self.log: list[tuple[str, EventEnvelope]] = []
        self.dead_letters: list[tuple[str, EventEnvelope, str]] = []
        self.deliver = deliver
        self.max_attempts = max_delivery_attempts
        self._lock = asyncio.Lock()

    def subscribe(self, topic: str, name: str, handler: Handler) -> None:
        self.subs[topic].append((name, handler))

    async def publish(self, topic: str, event: EventEnvelope) -> str:
        self.log.append((topic, event))
        _pub.inc(topic=topic)
        if self.deliver:
            for name, h in list(self.subs.get(topic, [])):
                await self._deliver(topic, name, h, event)
        return event.event_id

    async def _deliver(self, topic: str, name: str, h: Handler, event: EventEnvelope) -> None:
        err = ""
        for attempt in range(1, self.max_attempts + 1):
            try:
                await h(event)
                return
            except PermanentError as e:
                err = f"permanent: {e}"
                break
            except Exception as e:  # transient -> redelivery
                err = f"{type(e).__name__}: {e}"
                log.warning("delivery failed", topic=topic, sub=name, attempt=attempt, error=err)
        self.dead_letters.append((name, event, err))
        _dlq.inc(topic=topic)

    def events(self, topic: str) -> list[EventEnvelope]:
        return [e for t, e in self.log if t == topic]


class PubSubBus:
    """Google Pub/Sub publisher (message ordering by customer for per-subscriber sequencing)."""

    def __init__(self, project: str, prefix: str = ""):
        from google.cloud import pubsub_v1  # lazy: only needed on GCP
        self._pubsub = pubsub_v1
        # PUBSUB_EMULATOR_HOST (docker-compose) is honoured automatically by the client library
        self._client = pubsub_v1.PublisherClient(publisher_options=pubsub_v1.types.PublisherOptions(enable_message_ordering=True))
        self.project, self.prefix = project, prefix

    def topic_path(self, topic: str) -> str:
        return self._client.topic_path(self.project, f"{self.prefix}{topic}")

    async def publish(self, topic: str, event: EventEnvelope) -> str:
        data = event.model_dump_json().encode()
        attrs = {"type": event.type, "event_id": event.event_id, "source": event.source}
        kw = {"ordering_key": event.ordering_key} if event.ordering_key else {}
        fut = self._client.publish(self.topic_path(topic), data, **attrs, **kw)
        msg_id = await asyncio.get_running_loop().run_in_executor(None, fut.result, 10)
        _pub.inc(topic=topic)
        return msg_id


def make_bus(settings: Settings | None = None) -> EventBus:
    s = settings or get_settings()
    if s.bus_backend == "pubsub":
        return PubSubBus(s.gcp_project, s.pubsub_topic_prefix)
    return InMemoryBus(deliver=False)


def decode_push(body: dict) -> EventEnvelope:
    """Decode a Pub/Sub push request body into an EventEnvelope (raises PermanentError if malformed)."""
    try:
        msg = body["message"]
        raw = base64.b64decode(msg["data"]).decode()
        return EventEnvelope.model_validate(json.loads(raw))
    except Exception as e:
        raise PermanentError(f"malformed push message: {e}") from e


def encode_push(event: EventEnvelope, subscription: str = "local") -> dict:
    """Inverse of decode_push - used by the local runner & tests to simulate Pub/Sub push."""
    return {"message": {"data": base64.b64encode(event.model_dump_json().encode()).decode(),
                        "messageId": event.event_id, "attributes": {"type": event.type}},
            "subscription": subscription}


async def dead_letter(bus: EventBus, event: EventEnvelope, reason: str, source: str) -> None:
    dl = EventEnvelope(type="dead_letter", source=source, trace_id=event.trace_id,
                       payload={"reason": reason, "original": event.model_dump(mode="json")})
    await bus.publish(TOPIC_DLQ, dl)
    _dlq.inc(topic=event.type)

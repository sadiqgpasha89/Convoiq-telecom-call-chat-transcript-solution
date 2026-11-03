#!/usr/bin/env python
"""Create topics + push subscriptions on the Pub/Sub emulator (docker-compose). Idempotent."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from google.api_core.exceptions import AlreadyExists  # noqa: E402
from google.cloud import pubsub_v1  # noqa: E402
from google.cloud.pubsub_v1 import types  # noqa: E402

from convoiq.platform import topology  # noqa: E402


def main() -> None:
    project = os.environ.get("CONVOIQ_GCP_PROJECT", "convoiq-local")
    prefix = os.environ.get("CONVOIQ_PUBSUB_TOPIC_PREFIX", "")
    pub, sub = pubsub_v1.PublisherClient(), pubsub_v1.SubscriberClient()
    for _ in range(30):                                   # emulator may still be starting
        try:
            for t in topology.TOPICS:
                try:
                    pub.create_topic(name=pub.topic_path(project, prefix + t))
                except AlreadyExists:
                    pass
            break
        except Exception as e:
            print("waiting for emulator:", type(e).__name__)
            time.sleep(1)
    dlq = pub.topic_path(project, prefix + "convoiq-dead-letter")
    try:
        sub.create_subscription(name=sub.subscription_path(project, prefix + "dead-letter-pull"), topic=dlq)
    except AlreadyExists:
        pass
    for s in topology.SUBSCRIPTIONS:
        endpoint = f"http://{s['service']}:8080{s['path']}"
        try:
            sub.create_subscription(request={
                "name": sub.subscription_path(project, prefix + s["name"]), "topic": pub.topic_path(project, prefix + s["topic"]),
                "push_config": types.PushConfig(push_endpoint=endpoint), "ack_deadline_seconds": 60,
                "enable_message_ordering": True,
                "retry_policy": types.RetryPolicy(minimum_backoff={"seconds": 2}, maximum_backoff={"seconds": 30})})
            print("created", s["name"], "->", endpoint)
        except AlreadyExists:
            pass


if __name__ == "__main__":
    main()

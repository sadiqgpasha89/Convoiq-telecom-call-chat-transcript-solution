"""Infra <-> code consistency: Pub/Sub topology, generated Terraform inputs, push paths, publishers."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pytest

from convoiq.contracts.events import TOPIC_DLQ
from convoiq.platform import topology

from .conftest import MODELS, ROOT, needs_models

SVC_ATTR = {"redaction": "redaction", "enrichment": "enrichment", "orchestrator": "orchestrator", "action-broker": "broker",
            "analytics": "analytics", "monitor": "monitor"}


def test_generated_infra_files_are_current():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "gen_infra.py"), "--check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_subscription_names_unique_and_topics_known():
    names = [s["name"] for s in topology.SUBSCRIPTIONS]
    assert len(names) == len(set(names))
    assert {s["topic"] for s in topology.SUBSCRIPTIONS} <= set(topology.TOPICS)
    assert {s["service"] for s in topology.SUBSCRIPTIONS} <= set(topology.SERVICE_MODULES)
    assert all(s["path"].startswith("/pubsub/") for s in topology.SUBSCRIPTIONS)


def test_service_modules_exist_and_expose_create_factory():
    import importlib
    for sid, mod in topology.SERVICE_MODULES.items():
        m = importlib.import_module(f"convoiq.services.{mod}")
        assert callable(getattr(m, "create")), sid


@needs_models
def test_every_push_path_exists_on_its_service_and_matches_inprocess_subscriptions(bundle):
    from convoiq.config import Settings
    from convoiq.runtime.local import LocalSystem
    sysm = LocalSystem(Settings(env="local", auth_mode="none", models_dir=str(MODELS)), bundle=bundle)
    live = defaultdict(set)
    for sid, attr in SVC_ATTR.items():
        svc = getattr(sysm, attr)
        routes = {r.path for r in svc.app.routes}
        for s in topology.SUBSCRIPTIONS:
            if s["service"] == sid:
                assert s["path"] in routes, f"{sid} has no route {s['path']}"
                live[sid].add(s["topic"])
        assert set(svc.subs) == live[sid], f"{sid}: in-process subscriptions differ from topology"


@needs_models
def test_declared_publishers_cover_observed_publishers(bundle, pipeline):
    """Run a representative traffic mix and check every publish came from a service allowed to publish that topic (IAM would otherwise 403 in prod)."""
    import asyncio

    from convoiq.config import Settings
    from convoiq.data.seed import seed_for_conv
    from convoiq.data.synth import generate
    from convoiq.pipeline import corpus_to_raw
    from convoiq.runtime.local import LocalSystem

    async def main():
        sysm = LocalSystem(Settings(env="local", auth_mode="none", models_dir=str(MODELS)), bundle=bundle)
        convs = generate(40, seed=31)
        sim = sysm.client("sim")
        for c in convs:
            pid, prof, bills, tower = seed_for_conv(c, sysm.redactor.tok)
            if pid:
                await sim.post("/admin/seed/customer", json={"pseudo_id": pid, "profile": prof, "bills": bills})
            if tower:
                await sim.post("/admin/seed/tower", json=tower)
            await sysm.client("gateway").post("/v1/interactions", content=corpus_to_raw(c).model_dump_json(), headers={"content-type": "application/json"})
        seen = defaultdict(set)
        for topic, ev in sysm.bus.log:
            seen[topic].add(ev.source)
        return seen
    seen = asyncio.run(main())
    for topic, sources in seen.items():
        assert sources <= set(topology.PUBLISHERS[topic]), f"{topic}: undeclared publishers {sources - set(topology.PUBLISHERS[topic])}"


def test_terraform_references_only_known_generated_inputs():
    tf = (ROOT / "infra" / "terraform")
    if not tf.exists():
        pytest.skip("terraform dir not present")
    text = "\n".join(p.read_text() for p in tf.glob("*.tf"))
    for fn in re.findall(r'generated/([a-z_]+\.json)', text):
        assert (tf / "generated" / fn).exists(), fn

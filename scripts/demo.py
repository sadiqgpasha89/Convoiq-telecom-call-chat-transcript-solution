#!/usr/bin/env python
"""Push synthetic telecom conversations through a running ConvoIQ stack and print what the platform derived.

    python scripts/demo.py --base http://localhost:8080 [--sim http://localhost:8090] [--n 60] [--key <api-key>]

Seeds the simulated BSS/OSS with the facts each conversation refers to, ingests the raw transcripts through the public gateway,
waits for the asynchronous pipeline to settle, then prints headline insights, pending approvals and a sample analysis.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from convoiq.data.seed import seed_for_conv  # noqa: E402
from convoiq.data.synth import generate  # noqa: E402
from convoiq.pipeline import corpus_to_raw  # noqa: E402
from convoiq.privacy import Tokenizer  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8080")
    ap.add_argument("--sim", default="", help="telecom-sim base URL (default: <base>/_sim, falling back to http://localhost:8090)")
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--key", default="", help="API key / bearer token if the stack has auth enabled")
    ap.add_argument("--hmac-key", default="local-dev-hmac-key-0123456789", help="must equal the stack's CONVOIQ_PII_HMAC_KEY")
    a = ap.parse_args()
    H = {"content-type": "application/json", **({"authorization": f"Bearer {a.key}"} if a.key else {})}
    gw = httpx.Client(base_url=a.base, headers=H, timeout=60)
    sim_base = a.sim or (a.base.rstrip("/") + "/_sim")
    sim = httpx.Client(base_url=sim_base, headers=H, timeout=30)
    if sim.get("/healthz").status_code != 200:
        sim = httpx.Client(base_url="http://localhost:8090", headers=H, timeout=30)

    convs = generate(a.n, seed=a.seed)
    tok = Tokenizer(a.hmac_key)
    for c in convs:
        pid, prof, bills, tower = seed_for_conv(c, tok)
        if pid:
            sim.post("/admin/seed/customer", json={"pseudo_id": pid, "profile": prof, "bills": bills}).raise_for_status()
        if tower:
            sim.post("/admin/seed/tower", json=tower).raise_for_status()
    print(f"seeded BSS/OSS for {len(convs)} conversations; ingesting ...")
    ids = []
    for c in convs:
        raw = corpus_to_raw(c)
        r = gw.post("/v1/interactions", content=raw.model_dump_json())
        r.raise_for_status()
        ids.append(raw.interaction_id)
    t0 = time.time()
    done = 0
    while time.time() - t0 < 90:
        done = sum(gw.get(f"/v1/interactions/{i}").json().get("stage") == "analyzed" for i in ids[:10])
        if done >= min(10, len(ids)):
            break
        time.sleep(2)
    time.sleep(3)
    for name in ("summary", "issues", "compliance", "churn", "root-causes"):
        r = gw.get(f"/v1/insights/{name}")
        print(f"\n== /v1/insights/{name} [{r.status_code}]")
        print(json.dumps(r.json(), indent=1, default=str)[:900])
    ap_r = gw.get("/v1/approvals")
    if ap_r.status_code == 200:
        print(f"\n== pending human approvals: {len(ap_r.json())}")
        for x in ap_r.json()[:3]:
            print("  ", x["type"], x.get("amount_usd"), "-", (x.get("rationale") or "")[:90])
    one = gw.get(f"/v1/interactions/{ids[0]}")
    print("\n== sample interaction:", json.dumps(one.json(), indent=1, default=str)[:1200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
"""Generate infra inputs from the code that owns them, so Terraform can never drift from the application.

    python scripts/gen_infra.py            # (re)write infra/terraform/generated/*.json
    python scripts/gen_infra.py --check    # exit 1 if the committed files are stale (CI)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from convoiq.platform import schemas, topology  # noqa: E402

OUT = ROOT / "infra" / "terraform" / "generated"


def build() -> dict[str, str]:
    tables = {t: {"partition_field": d["partition"], "clustering": d["cluster"], "schema": schemas.bq_schema(t)} for t, d in schemas.TABLES.items()}
    topo = {"topics": topology.TOPICS, "subscriptions": topology.SUBSCRIPTIONS, "publishers": topology.PUBLISHERS,
            "max_delivery_attempts": topology.MAX_DELIVERY_ATTEMPTS, "services": topology.SERVICE_MODULES}
    return {"bq_tables.json": json.dumps(tables, indent=2, sort_keys=True) + "\n", "pubsub_topology.json": json.dumps(topo, indent=2, sort_keys=True) + "\n"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    files = build()
    stale = []
    for name, text in files.items():
        p = OUT / name
        if args.check:
            if not p.exists() or p.read_text() != text:
                stale.append(name)
        else:
            OUT.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
            print("wrote", p.relative_to(ROOT))
    if stale:
        print("STALE generated infra files (run `make infra-gen`):", ", ".join(stale))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

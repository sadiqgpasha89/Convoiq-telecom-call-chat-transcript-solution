#!/usr/bin/env python3
"""Canary health gate: compare the new revision's 5xx ratio (Cloud Monitoring, last N seconds) with a fixed budget.

Exit 0 = healthy (or not enough traffic to judge - reported, not hidden), 1 = unhealthy -> release.sh rolls back.
Stdlib only: runs inside the stock cloud-sdk build image using the metadata-server token.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

MIN_REQUESTS = 20          # below this the ratio is statistically meaningless
MAX_5XX_RATIO = 0.02


def token() -> str:
    req = urllib.request.Request("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
                                 headers={"Metadata-Flavor": "Google"})
    return json.load(urllib.request.urlopen(req, timeout=5))["access_token"]


def counts(project: str, service: str, revision: str, window_s: int) -> dict[str, float]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(seconds=window_s + 60)
    q = {
        "filter": ('metric.type="run.googleapis.com/request_count" AND resource.type="cloud_run_revision" '
                   f'AND resource.labels.service_name="{service}" AND resource.labels.revision_name="{revision}"'),
        "interval.startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "interval.endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "aggregation.alignmentPeriod": f"{window_s + 60}s", "aggregation.perSeriesAligner": "ALIGN_SUM",
        "aggregation.crossSeriesReducer": "REDUCE_SUM", "aggregation.groupByFields": "metric.labels.response_code_class",
    }
    url = f"https://monitoring.googleapis.com/v3/projects/{project}/timeSeries?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token()}"})
    data = json.load(urllib.request.urlopen(req, timeout=30))
    out: dict[str, float] = {}
    for ts in data.get("timeSeries", []):
        cls = ts["metric"]["labels"].get("response_code_class", "?")
        out[cls] = out.get(cls, 0.0) + sum(float(p["value"].get("int64Value", p["value"].get("doubleValue", 0))) for p in ts["points"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--service", required=True)
    ap.add_argument("--revision", required=True)
    ap.add_argument("--window-s", type=int, default=300)
    a = ap.parse_args()
    for attempt in range(3):               # monitoring metrics lag ~1-3 minutes
        try:
            c = counts(a.project, a.service, a.revision, a.window_s)
            break
        except Exception as e:                # noqa: BLE001
            print(f"monitoring query failed ({type(e).__name__}: {e}); retry {attempt + 1}/3", file=sys.stderr)
            time.sleep(30)
    else:
        print("could not read canary metrics -> treating as UNHEALTHY (fail closed)")
        return 1
    total, bad = sum(c.values()), c.get("5xx", 0.0)
    ratio = bad / total if total else 0.0
    print(f"{a.service}@{a.revision}: requests={total:.0f} 5xx={bad:.0f} ratio={ratio:.4f} by_class={c}")
    if total < MIN_REQUESTS:
        print(f"WARNING: only {total:.0f} requests observed (<{MIN_REQUESTS}); canary not statistically conclusive - promoting on startup-probe evidence only")
        return 0
    if ratio > MAX_5XX_RATIO:
        print(f"UNHEALTHY: 5xx ratio {ratio:.2%} > {MAX_5XX_RATIO:.0%}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

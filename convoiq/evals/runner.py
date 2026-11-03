"""Eval runner:  python -m convoiq.evals.runner --models models_eval --out reports [--suites a,b] [--quick]

Exit code 1 if any threshold in evals/thresholds.yaml is violated (CI / Cloud Build gate before promotion)."""
from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any

import yaml

from convoiq.config import ROOT
from convoiq.evals.context import EvalContext

SUITES = ["redaction", "classification", "security", "compliance", "agents", "rag", "anomaly", "drift", "robustness", "performance", "value"]
HEADLINES = {
    "redaction": "Privacy", "classification": "Model quality", "security": "Security & quarantine", "compliance": "Compliance rules",
    "agents": "Agent mesh", "rag": "Retrieval", "anomaly": "Outage detection", "drift": "Drift monitor", "robustness": "ASR noise & code-switching",
    "performance": "Latency", "value": "Business value (expected)"}


def check(report: dict, thresholds: dict) -> list[dict]:
    out = []
    for suite, metrics in thresholds.items():
        for name, rule in (metrics or {}).items():
            v = (report.get(suite) or {}).get(name)
            ok, why = True, ""
            if v is None:
                ok, why = False, "metric missing"
            else:
                if "min" in rule and v < rule["min"]:
                    ok, why = False, f"{v:.4g} < min {rule['min']}"
                if "max" in rule and v > rule["max"]:
                    ok, why = False, f"{v:.4g} > max {rule['max']}"
                if "equals" in rule and v != rule["equals"]:
                    ok, why = False, f"{v} != {rule['equals']}"
            out.append({"suite": suite, "metric": name, "value": v, "rule": rule, "ok": ok, "why": why})
    return out


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:.4f}" if abs(v) < 1000 else f"{v:,.0f}"
    if isinstance(v, (list, dict)):
        s = json.dumps(v, default=str)
        return s if len(s) < 140 else s[:137] + "..."
    return str(v)


def markdown(report: dict, checks: list[dict], meta: dict) -> str:
    L = [f"# ConvoIQ evaluation report", "", f"*generated {meta['generated_at']} · models `{meta['models_dir']}` (trained on tau2 **{meta['model_split']}** split) · "
         f"{meta['duration_s']:.0f}s*", ""]
    failed = [c for c in checks if not c["ok"]]
    L += [f"**Gates:** {len(checks) - len(failed)}/{len(checks)} passed" + ("" if not failed else " — FAILED: " + ", ".join(f"{c['suite']}.{c['metric']} ({c['why']})" for c in failed)), ""]
    L += ["> Reading guide — *tau2* numbers are on the official held-out test tasks with models trained on train tasks only. *synthetic* numbers come from our "
          "template generator (different seed than training, but same templates): they validate plumbing and rule logic, **not** real-world accuracy. "
          "tau2 conversations are LLM-simulated, not human calls.", ""]
    by = {}
    for c in checks:
        by.setdefault(c["suite"], {})[c["metric"]] = c
    for s in SUITES:
        if s not in report:
            continue
        L += [f"## {HEADLINES.get(s, s)}", "", "| metric | value | gate |", "|---|---|---|"]
        for k, v in report[s].items():
            if k.startswith("_"):
                continue
            c = by.get(s, {}).get(k)
            gate = "" if c is None else (f"✅ {json.dumps(c['rule'])}" if c["ok"] else f"❌ {c['why']}")
            L.append(f"| `{k}` | {_fmt(v)} | {gate} |")
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=str(ROOT / "models_eval"))
    ap.add_argument("--out", default=str(ROOT / "reports"))
    ap.add_argument("--suites", default="all")
    ap.add_argument("--thresholds", default=str(ROOT / "evals" / "thresholds.yaml"))
    ap.add_argument("--quick", action="store_true", help="smaller datasets (CI smoke)")
    ap.add_argument("--n-syn", type=int, default=0)
    args = ap.parse_args()

    ctx = EvalContext(models_dir=args.models, n_syn=args.n_syn or (200 if args.quick else 600), n_tau2_test=60 if args.quick else None)
    wanted = SUITES if args.suites == "all" else args.suites.split(",")
    report: dict[str, Any] = {}
    t0 = time.time()
    for s in wanted:
        t1 = time.time()
        try:
            report[s] = importlib.import_module(f"convoiq.evals.suites.{s}").run(ctx)
            print(f"[eval] {s:15s} ok  {time.time() - t1:5.1f}s", flush=True)
        except Exception:
            traceback.print_exc()
            report[s] = {"_error": traceback.format_exc()[-600:]}
            print(f"[eval] {s:15s} FAILED", flush=True)
    thresholds = yaml.safe_load(Path(args.thresholds).read_text()) if Path(args.thresholds).exists() else {}
    checks = check(report, {k: v for k, v in thresholds.items() if k in wanted})
    meta = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime()), "models_dir": args.models, "duration_s": time.time() - t0,
            "model_split": ctx._cache.get("model_split", "?"), "quick": args.quick}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "eval_report.json").write_text(json.dumps({"meta": meta, "report": report, "checks": checks}, indent=2, default=str))
    (out / "EVAL_REPORT.md").write_text(markdown(report, checks, meta))
    bad = [c for c in checks if not c["ok"]]
    errs = [s for s, r in report.items() if "_error" in r]
    print(f"[eval] {len(checks) - len(bad)}/{len(checks)} gates passed; suite errors: {errs or 'none'}")
    for c in bad:
        print(f"  FAIL {c['suite']}.{c['metric']}: {c['why']}")
    return 1 if bad or errs else 0


if __name__ == "__main__":
    sys.exit(main())

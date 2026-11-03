"""Latency / throughput of the CPU stages (single process, single thread).  Production scales horizontally on Cloud Run."""
from __future__ import annotations

import asyncio
import time

from convoiq.evals.context import EvalContext
from convoiq.evals.metrics import percentile
from convoiq.pipeline import corpus_to_raw


def run(ctx: EvalContext) -> dict:
    convs = ctx.synth(n=200, seed=ctx.seed + 51) + ctx.tau2_test[:60]
    pl = ctx.pipeline
    t_red, t_enr, t_ana = [], [], []
    loop = asyncio.new_event_loop()
    t_all = time.perf_counter()
    try:
        for c in convs:
            raw = corpus_to_raw(c)
            t0 = time.perf_counter(); red = pl.redact(raw)
            t1 = time.perf_counter(); enr = pl.enrich(red)
            t2 = time.perf_counter(); loop.run_until_complete(pl.orchestrator.analyze(enr))
            t3 = time.perf_counter()
            t_red.append((t1 - t0) * 1000); t_enr.append((t2 - t1) * 1000); t_ana.append((t3 - t2) * 1000)
    finally:
        loop.close()
    wall = time.perf_counter() - t_all
    res = {"n": len(convs), "throughput_conv_per_s_single_thread": len(convs) / wall}
    for name, v in (("redaction", t_red), ("enrichment", t_enr), ("orchestrator", t_ana)):
        res[f"{name}_p50_ms"], res[f"{name}_p95_ms"] = percentile(v, 50), percentile(v, 95)
    res["end_to_end_p95_ms"] = percentile([a + b + c for a, b, c in zip(t_red, t_enr, t_ana)], 95)
    return res

"""Drift monitor sensitivity / specificity: clean same-distribution windows must not ALERT; injected shifts must."""
from __future__ import annotations

import numpy as np

from convoiq.evals.context import EvalContext
from convoiq.monitor import DriftMonitor
from convoiq.pipeline import corpus_to_raw
from convoiq.services.analytics import interaction_row


def _rows(ctx: EvalContext, convs: list[dict]):
    rows, embs = [], []
    for c in convs:
        enr = ctx.pipeline.enrich(ctx.pipeline.redact(corpus_to_raw(c)))
        rows.append(interaction_row(enr))
        embs.append(enr.embedding)
    return rows, np.asarray(embs, float)


def run(ctx: EvalContext) -> dict:
    dm = DriftMonitor.from_file(f"{ctx.models_dir}/reference_distributions.json", ctx.settings)
    res: dict = {}

    def summarize(name: str, convs: list[dict]) -> None:
        rows, embs = _rows(ctx, convs)
        ms = dm.evaluate(rows, embs)
        res[f"{name}_alerts"] = sorted(m.metric for m in ms if m.status == "ALERT")
        res[f"{name}_warns"] = sorted(m.metric for m in ms if m.status == "WARN")
        res[f"{name}_n_alerts"] = len(res[f"{name}_alerts"])
        res[f"{name}_recommendation"] = dm.recommendation(ms).split(":")[0]
        for m in ms:
            res[f"{name}__{m.metric}"] = round(m.value, 4)

    # clean windows drawn like the training pool (synthetic mix + held-out tau2 test conversations)
    clean = ctx.synth(n=250, seed=ctx.seed + 31, seeded=False) + ctx.tau2_test[:120]
    summarize("clean", clean)
    # shift 1: issue-mix shift (billing/churn storm)
    summarize("mix_shift", ctx.synth(n=300, seed=ctx.seed + 32, families=["billing_dispute", "churn_port_out"], seeded=False))
    # shift 2: channel degradation (heavy ASR noise) - covariate shift in text
    summarize("asr_noise_0.35", ctx.synth(n=300, seed=ctx.seed + 33, noise=0.35, seeded=False))
    res["clean_false_alarm"] = float(res["clean_n_alerts"] > 0)
    res["mix_shift_detected"] = float(res["mix_shift_n_alerts"] > 0)
    res["asr_shift_detected"] = float(res["asr_noise_0.35_n_alerts"] > 0 or len(res["asr_noise_0.35_warns"]) > 0)
    return res

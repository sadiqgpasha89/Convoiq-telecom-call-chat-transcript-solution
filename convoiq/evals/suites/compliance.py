"""Per-rule precision / recall of the compliance agent against labelled violations (synthetic), plus false-positive rates on tau2."""
from __future__ import annotations

import asyncio
from collections import Counter

from convoiq.agents.compliance import RULES
from convoiq.evals.context import EvalContext
from convoiq.pipeline import corpus_to_raw

MEASURED = [r.id for r in RULES if r.measured]


def _prf(tp: int, fp: int, fn: int) -> dict[str, float]:
    p, r = tp / max(tp + fp, 1), tp / max(tp + fn, 1)
    return {"precision": p, "recall": r, "f1": 2 * p * r / max(p + r, 1e-9)}


def _eval(ctx: EvalContext, convs: list[dict]) -> dict:
    tp, fp, fn = Counter(), Counter(), Counter()
    support = Counter()
    for c in convs:
        red = ctx.pipeline.redact(corpus_to_raw(c))
        enr = ctx.pipeline.enrich(red)
        from convoiq.agents.compliance import check_compliance
        pred = {v.rule_id for v in check_compliance(enr, ctx.settings).violations}
        gold = set(c["labels"]["violations"])
        for rid in MEASURED:
            tp[rid] += rid in pred and rid in gold
            fp[rid] += rid in pred and rid not in gold
            fn[rid] += rid not in pred and rid in gold
            support[rid] += rid in gold
    out = {}
    for rid in MEASURED:
        m = _prf(tp[rid], fp[rid], fn[rid])
        out[rid.split("_")[0]] = {**m, "support": support[rid]}
    out["macro_f1"] = sum(v["f1"] for k, v in out.items() if isinstance(v, dict)) / len(MEASURED)
    return out


def run(ctx: EvalContext) -> dict:
    res: dict = {}
    clean = _eval(ctx, ctx.synth(n=max(ctx.n_syn, 500), seed=ctx.seed + 9))
    for k, v in clean.items():
        if isinstance(v, dict):
            for m, x in v.items():
                res[f"{k}_{m}"] = x
        else:
            res[f"clean_{k}"] = v
    # unmeasured rules: how often they fire on tau2 (should be near zero - agents are polite scripted LLMs); informational + FP smoke test
    from convoiq.agents.compliance import check_compliance
    cnt = Counter()
    for c in ctx.tau2_test[:300]:
        enr = ctx.pipeline.enrich(ctx.pipeline.redact(corpus_to_raw(c)))
        for v in check_compliance(enr, ctx.settings).violations:
            cnt[v.rule_id.split("_")[0]] += 1
    n = min(300, len(ctx.tau2_test))
    for rid in [r.id.split("_")[0] for r in RULES]:
        res[f"tau2_flag_rate_{rid}"] = cnt[rid] / n
    res["measured_rules"] = [r.id for r in RULES if r.measured]
    res["unmeasured_rules"] = [r.id for r in RULES if not r.measured]
    return res

"""Robustness: ASR-noise sweep at a *measured* word-error rate, and code-switched (Hinglish / Spanglish) slices."""
from __future__ import annotations

import zlib

import numpy as np

from convoiq.data.noise import asr_noise, wer
from convoiq.evals.context import EvalContext
from convoiq.evals.metrics import macro_micro_f1
from convoiq.nlp.churn_signals import churn_signals
from convoiq.nlp.text import customer_turns
from convoiq.pipeline import corpus_to_raw
from convoiq.training.datasets import prep


def _slice(ctx: EvalContext, convs: list[dict]) -> dict:
    y, p = [], []
    churn_tp = churn_fn = inj_tp = inj_n = 0
    for c in convs:
        turns = prep(c)
        pr = ctx.bundle.issue.predict(turns)
        y.append(c["issue_type"]); p.append(max(pr, key=pr.get))
        if c["labels"]["churn_intent"]:
            s = churn_signals(turns)
            hit = s.cancel_intent or s.port_out_request or bool(s.competitor_mentions and s.price_complaint)
            churn_tp += hit; churn_fn += not hit
        if c["labels"]["injection"]:
            inj_n += 1
            inj_tp += max([ctx.bundle.injection.score(t.text)[0] for t in customer_turns(turns)] or [0]) >= 0.5
    return {"issue_macro_f1": macro_micro_f1(y, p)["macro_f1"], "issue_accuracy": macro_micro_f1(y, p)["accuracy"],
            "churn_recall": churn_tp / max(churn_tp + churn_fn, 1), "injection_recall": inj_tp / max(inj_n, 1), "n": len(convs)}


def run(ctx: EvalContext) -> dict:
    res: dict = {}
    base = ctx.synth(n=300, seed=ctx.seed + 41, seeded=False)
    for rate in (0.0, 0.1, 0.2, 0.3):
        noisy = ctx.synth(n=300, seed=ctx.seed + 41, noise=rate, seeded=False)
        # reference = same text with only the (deterministic) ASR normalisation applied: lowercase, no punctuation, numbers spoken
        assert [c["conv_id"] for c in base] == [c["conv_id"] for c in noisy], "noise must not change the underlying conversation"
        w = [wer(asr_noise(a["text"], 0.0, seed=zlib.crc32(a["text"].encode())), b["text"])
             for ca, cb in zip(base, noisy) for a, b in zip(ca["turns"], cb["turns"])
             if a["role"] == "customer" and not any(x["turn"] == a["i"] for x in ca["labels"]["pii"])]
        r = _slice(ctx, noisy)
        tag = f"asr_{int(rate * 100):02d}"
        res[f"{tag}_measured_wer"] = float(np.mean(w)) if (w and rate > 0) else 0.0   # rate 0 = text channel, no ASR
        for k, v in r.items():
            res[f"{tag}_{k}"] = v
    res["asr_30_issue_f1_drop"] = res["asr_00_issue_macro_f1"] - res["asr_30_issue_macro_f1"]
    # code-switch slices
    cs = ctx.synth(n=600, seed=ctx.seed + 42, seeded=False)
    en = [c for c in cs if c["labels"]["code_switch"] == "none"]
    sw = [c for c in cs if c["labels"]["code_switch"] != "none"]
    r_en, r_sw = _slice(ctx, en), _slice(ctx, sw)
    res["en_issue_accuracy"], res["codeswitch_issue_accuracy"] = r_en["issue_accuracy"], r_sw["issue_accuracy"]
    res["codeswitch_churn_recall"], res["codeswitch_n"] = r_sw["churn_recall"], r_sw["n"]
    res["codeswitch_gap_accuracy"] = r_en["issue_accuracy"] - r_sw["issue_accuracy"]
    return res

"""Model quality: issue, root cause, resolution risk, churn, sentiment/sarcasm.  tau2 numbers use the OFFICIAL TEST SPLIT only
with models trained on the train split (models_eval/).  Synthetic numbers are template-derived and optimistic - reported separately."""
from __future__ import annotations

import numpy as np
from sklearn.metrics import roc_auc_score

from convoiq.evals.context import EvalContext
from convoiq.evals.metrics import binary_report, ece, macro_micro_f1, multilabel_f1
from convoiq.nlp.churn_signals import churn_signals
from convoiq.nlp.sentiment import analyze_sentiment
from convoiq.nlp.text import customer_turns
from convoiq.pipeline import corpus_to_raw
from convoiq.training.datasets import prep


def _issue(ctx: EvalContext, convs: list[dict]) -> dict:
    y, pred, conf = [], [], []
    for c in convs:
        p = ctx.bundle.issue.predict(prep(c))
        k = max(p, key=p.get)
        y.append(c["issue_type"]); pred.append(k); conf.append(p[k])
    r = macro_micro_f1(y, pred)
    r["ece"] = ece(np.array([a == b for a, b in zip(y, pred)], float), np.array(conf))
    r["n"] = len(convs)
    return r


def run(ctx: EvalContext) -> dict:
    res: dict = {}
    t2 = ctx.tau2_test
    for k, v in _issue(ctx, t2).items():
        res[f"issue_tau2_test_{k}"] = v
    syn = ctx.synth(seeded=False)
    for k, v in _issue(ctx, syn).items():
        res[f"issue_synthetic_{k}"] = v

    # --- root cause (multi-label), tau2 test ------------------------------------------------------------------------------
    gold = [set(c["root_causes"]) for c in t2]
    rcm = ctx.bundle.rootcause
    pred = [{t for t, p in rcm.predict(prep(c)) if p >= rcm.thresholds.get(t, 0.5)} for c in t2]
    r = multilabel_f1(gold, pred)
    res["rootcause_tau2_test_micro_f1"] = r["micro_f1"]
    res["rootcause_tau2_test_macro_f1"] = r["macro_f1"]
    res["rootcause_tau2_test_exact_match"] = float(np.mean([g == p for g, p in zip(gold, pred)]))
    res["rootcause_tau2_test_any_hit"] = float(np.mean([bool(g & p) for g, p in zip(gold, pred)]))

    # --- resolution risk (tech-support domain only) ----------------------------------------------------------------------------
    app = [c for c in t2 if c["issue_type"] in ctx.bundle.resolution.applicable]
    y = np.array([0 if c["outcome"]["resolved"] else 1 for c in app])
    p_final = np.array([ctx.bundle.resolution.predict(prep(c), True, c["issue_type"])[0] for c in app])
    br = binary_report(y, p_final)
    res.update({f"resolution_final_{k}": v for k, v in br.items()})
    p_early = np.array([ctx.bundle.resolution.predict(prep(c)[: max(2, int(len(prep(c)) * 0.4))], False, c["issue_type"])[0] for c in app])
    if 0 < y.sum() < len(y):
        res["resolution_early40_auc"] = float(roc_auc_score(y, p_early))
    res["resolution_base_rate_unresolved"] = float(y.mean())

    # --- churn (tabular, CV on IBM data; conversation signals on synthetic) ----------------------------------------------------------
    cm = (ctx.bundle.cards.get("churn") or {}).get("metrics") or {}
    for k in ("auc", "pr_auc", "brier", "ece", "lift_at_10"):
        if k in cm:
            res[f"churn_tabular_cv_{k}"] = cm[k]
    cs = ctx.synth(seeded=False)
    tp = fp = fn = tn = 0
    comp_ok = comp_n = 0
    for c in cs:
        s = churn_signals(prep(c))
        pred_pos = s.cancel_intent or s.port_out_request
        gold_pos = c["labels"]["churn_intent"]
        tp += pred_pos and gold_pos; fp += pred_pos and not gold_pos; fn += (not pred_pos) and gold_pos; tn += (not pred_pos) and not gold_pos
        if gold_pos and c["labels"].get("competitor"):
            comp_n += 1
            comp_ok += c["labels"]["competitor"].lower() in s.competitor_mentions
    res["churn_intent_precision"] = tp / max(tp + fp, 1)
    res["churn_intent_recall"] = tp / max(tp + fn, 1)
    res["churn_intent_false_positive_rate"] = fp / max(fp + tn, 1)
    res["competitor_extraction_accuracy"] = comp_ok / max(comp_n, 1)
    # hard negatives: competitor mentioned benignly
    hn = [c for c in cs if c["labels"]["family"] == "competitor_benign"]
    res["benign_competitor_false_alarm_rate"] = float(np.mean([bool(churn_signals(prep(c)).cancel_intent or churn_signals(prep(c)).port_out_request) for c in hn])) if hn else 0.0
    # real text, no churn intent expected: tau2 customers are troubleshooting
    res["tau2_churn_false_alarm_rate"] = float(np.mean([bool(churn_signals(prep(c)).cancel_intent or churn_signals(prep(c)).port_out_request) for c in t2]))

    # --- sentiment / sarcasm ----------------------------------------------------------------------------------------------------------
    trend_ok = 0
    sar_tp = sar_fp = sar_fn = 0
    gold_v, pred_v = [], []
    for c in cs:
        s = analyze_sentiment(prep(c))
        trend_ok += s.trend == c["labels"]["valence_trend"]
        sp, sg = s.sarcasm_score >= 0.55, c["labels"]["sarcasm"]
        sar_tp += sp and sg; sar_fp += sp and not sg; sar_fn += (not sp) and sg
        cust = customer_turns(prep(c))
        vals = [v for t, v in zip(c["turns"], c["labels"]["valence"]) if t["role"] == "customer" and v is not None]
        if len(vals) == len(s.per_turn):
            gold_v += vals; pred_v += s.per_turn
    res["sentiment_trend_accuracy"] = trend_ok / len(cs)
    res["sentiment_valence_pearson"] = float(np.corrcoef(gold_v, pred_v)[0, 1]) if len(gold_v) > 5 else None
    res["sarcasm_precision"] = sar_tp / max(sar_tp + sar_fp, 1)
    res["sarcasm_recall"] = sar_tp / max(sar_tp + sar_fn, 1)
    return res

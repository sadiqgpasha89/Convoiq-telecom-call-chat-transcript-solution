"""Train every classical model, with grouped cross-validation and model cards.

    python -m convoiq.training.train_all                       # production models -> models/
    python -m convoiq.training.train_all --split train --out models_eval   # eval-grade (official tau2 train tasks only)

Honest evaluation rules enforced here:
* tau2 conversations are split **by task** (GroupKFold / official split) - the same task appears under
  six agent models x four trials, so a random split would leak.
* Reported metrics are out-of-fold; the shipped model is refit on all training data.
* Synthetic data only supplies classes tau2 lacks; metrics are reported *per source*.
"""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.isotonic import IsotonicRegression
from sklearn.model_selection import GroupKFold, StratifiedKFold

from convoiq.config import ROOT
from convoiq.data.noise import asr_noise
from convoiq.evals import metrics as M
from convoiq.models.classical import (CHURN_DEFAULT_ROW, ChurnModel, InjectionClassifier, IssueClassifier, LsaEmbedder,
                                      ResolutionRiskModel, RootCauseModel)
from convoiq.models.features import STRUCT_NAMES, customer_view, model_text, struct_vector
from convoiq.models.registry import save_model
from convoiq.nlp.sentiment import analyze_sentiment
from convoiq.taxonomy import ROOT_CAUSES
from convoiq.training import injection_data as inj
from convoiq.training.datasets import data_hash, load_tau2, prep, split_tau2, synthetic, CORPUS, SAMPLE

VERSION = "2.0.0"
SEED = 0


def log(msg: str) -> None:
    print(f"[train {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def build_pool(split: str, n_syn: int, seed: int):
    """Unified labelled pool: tau2 (real-ish) + synthetic (enterprise intents)."""
    tau = split_tau2(load_tau2(), split)
    syn = synthetic(n_syn, seed=seed + 100)
    noisy = synthetic(n_syn // 4, seed=seed + 101, noise_rate=0.12)      # ASR-flavoured copies for robustness
    pool = []
    for c in tau:
        pool.append({"src": "tau2", "group": c["task_id"], "conv": c})
    for i, c in enumerate(syn + noisy):
        pool.append({"src": "synthetic", "group": c["conv_id"], "conv": c})
    return pool


# ---------------------------------------------------------------------------------------------
def train_issue(pool, out, dh):
    texts = [model_text(prep(p["conv"])) for p in pool]
    y = np.array([p["conv"]["issue_type"] for p in pool])
    g = np.array([p["group"] for p in pool])
    src = np.array([p["src"] for p in pool])
    oof_raw = np.zeros((len(y), len(np.unique(y))))
    classes = sorted(set(y))
    for tr, te in GroupKFold(5).split(texts, y, g):
        m = IssueClassifier().fit([texts[i] for i in tr], y[tr])
        P = m.clf.predict_proba(m.vec.transform([texts[i] for i in te]))
        for j, c in enumerate(m.classes_):
            oof_raw[te, classes.index(c)] = P[:, j]
    pred = np.array(classes)[oof_raw.argmax(1)]
    metrics = {"overall": M.macro_micro_f1(y, pred)}
    for s in ("tau2", "synthetic"):
        mk = src == s
        metrics[s] = M.macro_micro_f1(y[mk], pred[mk])
        metrics[s]["n"] = int(mk.sum())
    model = IssueClassifier().fit(texts, y)
    model.calibrate(texts, y)   # temperature on OOF would be better; use OOF probs below
    # temperature from OOF probabilities
    idx = np.array([classes.index(v) for v in y])
    bestT, best = 1.0, 1e9
    for T in np.linspace(0.5, 3.0, 26):
        z = np.log(np.clip(oof_raw, 1e-9, 1)) / T
        z -= z.max(1, keepdims=True)
        q = np.exp(z) / np.exp(z).sum(1, keepdims=True)
        nll = -np.log(np.clip(q[np.arange(len(y)), idx], 1e-9, 1)).mean()
        if nll < best:
            best, bestT = nll, T
    model.T = float(bestT)
    metrics["temperature"] = model.T
    metrics["confusion_pairs_top"] = Counter(f"{a}->{b}" for a, b in zip(y, pred) if a != b).most_common(6)
    save_model("issue", model, {"version": VERSION, "data_hash": dh, "cv": "GroupKFold(5) by task/conv", "metrics": metrics,
                                "classes": classes, "limitations": "synthetic classes are template-derived; re-validate on live traffic"}, out)
    log(f"issue: {metrics['overall']} tau2={metrics['tau2']['macro_f1']:.3f} syn={metrics['synthetic']['macro_f1']:.3f}")
    return model, metrics


def train_rootcause(pool, out, dh):
    texts = [model_text(prep(p["conv"])) for p in pool]
    Y = [set(p["conv"]["root_causes"]) for p in pool]
    g = np.array([p["group"] for p in pool])
    src = np.array([p["src"] for p in pool])
    labels = sorted({l for ys in Y for l in ys})
    oof = {l: np.zeros(len(pool)) for l in labels}
    for tr, te in GroupKFold(5).split(texts, Y, g):
        m = RootCauseModel().fit([texts[i] for i in tr], [Y[i] for i in tr])
        probs = m.predict_proba_texts([texts[i] for i in te])
        for l, p in probs.items():
            oof[l][te] = p
    model = RootCauseModel().fit(texts, Y)
    model.tune_thresholds(oof, Y)
    pred = [{l for l in labels if l in oof and oof[l][i] >= model.thresholds.get(l, 0.5)} for i in range(len(pool))]
    metrics = {}
    for s in ("tau2", "synthetic"):
        mk = np.where(src == s)[0]
        r = M.multilabel_f1([Y[i] for i in mk], [pred[i] for i in mk])
        metrics[s] = {k: v for k, v in r.items()}
        metrics[s]["n"] = int(len(mk))
    # early-warning variant: first 6 turns only (tau2 only)
    ti = np.where(src == "tau2")[0]
    t6 = [model_text(prep(pool[i]["conv"]), 6) for i in ti]
    oof6 = {l: np.zeros(len(ti)) for l in labels}
    gt = g[ti]
    Yt = [Y[i] for i in ti]
    for tr, te in GroupKFold(5).split(t6, Yt, gt):
        m6 = RootCauseModel().fit([t6[i] for i in tr], [Yt[i] for i in tr])
        for l, p in m6.predict_proba_texts([t6[i] for i in te]).items():
            oof6[l][te] = p
    pred6 = [{l for l in labels if l in oof6 and oof6[l][i] >= 0.5} for i in range(len(ti))]
    metrics["tau2_first6turns"] = {k: v for k, v in M.multilabel_f1(Yt, pred6).items() if k != "per_label"}
    save_model("rootcause", model, {"version": VERSION, "data_hash": dh, "metrics": metrics, "labels": labels,
                                    "thresholds": model.thresholds, "cv": "GroupKFold(5) by task/conv"}, out)
    log(f"rootcause tau2 micro-F1={metrics['tau2']['micro_f1']:.3f} macro={metrics['tau2']['macro_f1']:.3f} first6={metrics['tau2_first6turns']['micro_f1']:.3f}")
    return model, metrics


def train_resolution(pool, out, dh):
    tp = [p for p in pool if p["src"] == "tau2"]
    T = [prep(p["conv"]) for p in tp]
    y = np.array([int(not p["conv"]["outcome"]["resolved"]) for p in tp])
    g = np.array([p["group"] for p in tp])
    S = np.vstack([struct_vector(t) for t in T])
    oof_f = np.zeros(len(y))
    for tr, te in GroupKFold(5).split(S, y, g):
        m = HistGradientBoostingClassifier(max_depth=4, learning_rate=0.06, max_iter=250, random_state=SEED).fit(S[tr], y[tr])
        oof_f[te] = m.predict_proba(S[te])[:, 1]
    iso_f = IsotonicRegression(out_of_bounds="clip").fit(oof_f, y)
    metrics = {"final_raw": M.binary_report(y, oof_f), "final_calibrated_insample": M.binary_report(y, iso_f.predict(oof_f))}
    # early head on prefixes
    ks = [4, 6, 8, 12, 16]
    rows = []
    for i, t in enumerate(T):
        for k in ks:
            if len(t) > k:
                rows.append((i, k))
    texts_e = [model_text(T[i], k) for i, k in rows]
    S_e = np.vstack([struct_vector(T[i][:k]) for i, k in rows])
    y_e = np.array([y[i] for i, _ in rows])
    g_e = np.array([g[i] for i, _ in rows])
    k_e = np.array([k for _, k in rows])
    oof_e = np.zeros(len(rows))
    for tr, te in GroupKFold(5).split(texts_e, y_e, g_e):
        m = ResolutionRiskModel()
        m.fit_early([texts_e[i] for i in tr], S_e[tr], y_e[tr])
        oof_e[te] = m.early.predict_proba(m._early_X([texts_e[i] for i in te], S_e[te]))[:, 1]
    iso_e = IsotonicRegression(out_of_bounds="clip").fit(oof_e, y_e)
    metrics["early_by_prefix"] = {int(k): M.binary_report(y_e[k_e == k], oof_e[k_e == k]) for k in ks if (k_e == k).sum() > 50}
    model = ResolutionRiskModel()
    model.fit_final(S, y)
    model.iso_final = iso_f
    model.fit_early(texts_e, S_e, y_e)
    model.iso_early = iso_e
    model.base_rate = float(y.mean())
    save_model("resolution", model, {"version": VERSION, "data_hash": dh, "metrics": metrics, "applicable_issue_types": model.applicable,
                                     "struct_features": STRUCT_NAMES, "cv": "GroupKFold(5) by task; isotonic fit on OOF",
                                     "limitations": "trained on LLM-simulated tech-support dialogs only; outside that domain the model returns the base rate"}, out)
    log(f"resolution final AUC={metrics['final_raw']['roc_auc']:.3f} early@4 AUC={metrics['early_by_prefix'].get(4, {}).get('roc_auc')}")
    return model, metrics


def train_churn(out, dh):
    df = pd.read_csv(ROOT / "data" / "reference" / "telco_customer_churn.csv")
    df["TotalCharges"] = pd.to_numeric(df["TotalCharges"], errors="coerce").fillna(0.0)
    y = (df["Churn"] == "Yes").astype(int).values
    X = df.drop(columns=["customerID", "Churn"])
    cat = [c for c in X.columns if not pd.api.types.is_numeric_dtype(X[c])]
    num = [c for c in X.columns if c not in cat]
    from sklearn.compose import ColumnTransformer
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OrdinalEncoder
    def mk():
        pre = ColumnTransformer([("c", OrdinalEncoder(handle_unknown="use_encoded_value", unknown_value=-1), cat)], remainder="passthrough")
        gb = HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=220, l2_regularization=1.0, random_state=SEED,
                                            categorical_features=list(range(len(cat))))
        return Pipeline([("pre", pre), ("gb", gb)])
    Xo = X[cat + num]
    oof = np.zeros(len(y))
    for tr, te in StratifiedKFold(5, shuffle=True, random_state=SEED).split(Xo, y):
        oof[te] = mk().fit(Xo.iloc[tr], y[tr]).predict_proba(Xo.iloc[te])[:, 1]
    iso = IsotonicRegression(out_of_bounds="clip").fit(oof, y)
    metrics = {"oof_raw": M.binary_report(y, oof), "lift_at_10pct": M.lift_at_k(y, oof, 0.1),
               "oof_calibrated_insample": M.binary_report(y, iso.predict(oof))}
    pipe = mk().fit(Xo, y)
    defaults = {c: X[c].mode().iloc[0] for c in cat}
    defaults.update({c: float(X[c].median()) for c in num})
    defaults.update({k: v for k, v in CHURN_DEFAULT_ROW.items() if k in defaults and k in ("Contract", "InternetService", "PaymentMethod")})
    model = ChurnModel(pipeline=pipe, feature_names=cat + num, cat_cols=cat, num_cols=num, defaults=defaults, iso=iso, base_rate=float(y.mean()))
    save_model("churn", model, {"version": VERSION, "data_hash": data_hash(ROOT / "data/reference/telco_customer_churn.csv"),
                                "metrics": metrics, "cv": "StratifiedKFold(5)", "source": "IBM Telco Customer Churn (Apache-2.0)",
                                "limitations": "US-style telco survey data; fusion with conversation signals uses expert priors until "
                                               "30/60/90-day retention outcomes are available to recalibrate"}, out)
    log(f"churn AUC={metrics['oof_raw']['roc_auc']:.3f} PR-AUC={metrics['oof_raw']['pr_auc']:.3f} brier={metrics['oof_raw']['brier']:.3f}")
    return model, metrics


def train_injection(pool, out, dh):
    benign_tau = []
    for p in pool:
        if p["src"] == "tau2":
            benign_tau += [t["text"] for t in p["conv"]["turns"] if t["role"] == "customer" and t["text"]][:6]
    rng = np.random.default_rng(SEED)
    benign_tau = list(rng.choice(benign_tau, size=min(5000, len(benign_tau)), replace=False))
    benign_syn = [t["text"] for p in pool if p["src"] == "synthetic" for t in p["conv"]["turns"] if t["role"] == "customer"][:3000]
    benign_nm = inj.benign_near_miss(900, seed=1)
    B = benign_tau + benign_syn + benign_nm
    atk = inj.attacks(4000, seed=2)
    metrics = {"leave_one_family_out": {}}
    fams = list(inj.FAMILIES)
    rs = np.random.default_rng(1)
    for f in fams:
        train_a = [t for t, fam in atk if fam != f]
        test_a = [t for t, fam in inj.attacks(300, seed=77, families=[f])]
        clf = InjectionClassifier().fit(train_a + B, [1] * len(train_a) + [0] * len(B))
        full_scores = clf.score_many(test_a)
        # score without heuristics to show model-only generalisation
        model_only = clf.clf.predict_proba(clf.vec.transform(test_a))[:, 1]
        metrics["leave_one_family_out"][f] = {"recall_combined@0.5": float((full_scores >= 0.5).mean()),
                                              "recall_model_only@0.5": float((model_only >= 0.5).mean())}
    test_b = inj.benign_near_miss(400, seed=99)
    clf = InjectionClassifier().fit([t for t, _ in atk] + B, [1] * len(atk) + [0] * len(B))
    metrics["near_miss_false_positive_rate"] = float((clf.score_many(test_b) >= 0.5).mean())
    save_model("injection", clf, {"version": VERSION, "data_hash": dh, "metrics": metrics, "families": fams,
                                  "limitations": "generated attack corpus; keep red-teaming and add production-observed attacks"}, out)
    lo = [v["recall_combined@0.5"] for v in metrics["leave_one_family_out"].values()]
    log(f"injection LOFO recall min={min(lo):.3f} mean={np.mean(lo):.3f}  near-miss FPR={metrics['near_miss_false_positive_rate']:.3f}")
    return clf, metrics


def build_embedder_and_index(pool, out, dh, issue_model):
    texts = [customer_view(prep(p["conv"])) or model_text(prep(p["conv"])) for p in pool]
    emb = LsaEmbedder(64).fit(texts)
    save_model("embedder", emb, {"version": VERSION, "data_hash": dh, "dim": 64}, out)
    # case index: resolved tau2 conversations -> fixes actually performed
    cases = []
    for p in pool:
        if p["src"] != "tau2" or not p["conv"]["outcome"]["resolved"]:
            continue
        c = p["conv"]
        fixes = sorted({f"{tc['requestor']}:{tc['name']}" for t in c["turns"] for tc in t.get("tool_calls", [])
                        if tc["name"] in _FIX_TOOLS})
        cases.append({"conv_id": c["conv_id"], "task_id": c["task_id"], "issue_type": c["issue_type"], "root_causes": c["root_causes"],
                      "fixes": fixes, "text": customer_view(prep(c))})
    # one case per (task, fix-set) to keep the index compact
    seen, uniq = set(), []
    for c in cases:
        k = (c["task_id"], tuple(c["fixes"]))
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    vecs = emb.embed_texts([c["text"] for c in uniq])
    for c in uniq:
        c.pop("text")
    save_model("case_index", {"vectors": vecs.astype(np.float32), "cases": uniq}, {"version": VERSION, "data_hash": dh, "n": len(uniq)}, out)
    log(f"embedder + case index ({len(uniq)} resolved cases)")
    return emb


_FIX_TOOLS = {"toggle_airplane_mode", "toggle_data", "toggle_roaming", "toggle_data_saver_mode", "set_network_mode_preference",
              "reseat_sim_card", "reset_apn_settings", "reboot_device", "toggle_wifi_calling", "disconnect_vpn", "grant_app_permission",
              "enable_roaming", "refuel_data", "resume_line", "send_payment_request", "make_payment", "transfer_to_human_agents", "set_apn_settings"}


def reference_distributions(pool, out, emb, issue_model, dh):
    """Baselines for drift monitoring (monitor service compares live windows against these)."""
    convs = [p["conv"] for p in pool]
    ref = {"created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "data_hash": dh}
    cnt = Counter(c["issue_type"] for c in convs)
    n = sum(cnt.values())
    ref["issue_distribution"] = {k: v / n for k, v in cnt.items()}
    sents = np.array([analyze_sentiment(prep(c)).mean for c in convs])
    ref["sentiment_mean_quantiles"] = np.quantile(sents, np.linspace(0, 1, 21)).tolist()
    ref["n_turns_quantiles"] = np.quantile([len(c["turns"]) for c in convs], np.linspace(0, 1, 21)).tolist()
    # uniform random subsample (the pool is ordered by source - a prefix slice would silently drop whole sources)
    rng = np.random.default_rng(0)
    sub = [convs[i] for i in sorted(rng.choice(len(convs), min(1500, len(convs)), replace=False))]
    E = emb.embed_texts([customer_view(prep(c)) or "x" for c in sub])
    ref["embedding_centroid"] = E.mean(0).tolist()
    ref["embedding_sample"] = E[rng.choice(len(E), min(500, len(E)), replace=False)].round(4).tolist()
    vocab = set(emb.vec.vocabulary_)
    toks = [w for c in [convs[i] for i in rng.choice(len(convs), min(800, len(convs)), replace=False)] for t in c["turns"] if t["role"] == "customer" for w in t["text"].lower().split()]
    ref["oov_rate"] = float(np.mean([w.strip(".,!?") not in vocab for w in toks])) if toks else 0.0
    (Path(out) / "reference_distributions.json").write_text(json.dumps(ref))
    log("reference distributions written")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="all", choices=["all", "train"], help="tau2 task split to train on")
    ap.add_argument("--out", default=str(ROOT / "models"))
    ap.add_argument("--n-syn", type=int, default=3000)
    ap.add_argument("--only", default="", help="comma list: issue,rootcause,resolution,churn,injection,embedder")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    dh = data_hash(CORPUS if CORPUS.exists() else SAMPLE)
    only = set(filter(None, args.only.split(",")))
    want = lambda k: not only or k in only
    t0 = time.time()
    pool = build_pool(args.split, args.n_syn, args.seed)
    log(f"pool: {Counter(p['src'] for p in pool)}  split={args.split}")
    summary = {}
    issue_model = None
    if want("issue"):
        issue_model, summary["issue"] = train_issue(pool, out, dh)
    if want("rootcause"):
        _, summary["rootcause"] = train_rootcause(pool, out, dh)
    if want("resolution"):
        _, summary["resolution"] = train_resolution(pool, out, dh)
    if want("churn"):
        _, summary["churn"] = train_churn(out, dh)
    if want("injection"):
        _, summary["injection"] = train_injection(pool, out, dh)
    if want("embedder"):
        emb = build_embedder_and_index(pool, out, dh, issue_model)
        reference_distributions(pool, out, emb, issue_model, dh)
    (out / "MANIFEST.json").write_text(json.dumps({"version": VERSION, "split": args.split, "data_hash": dh,
                                                   "trained_in_s": round(time.time() - t0, 1)}, indent=2))
    log(f"done in {time.time() - t0:.0f}s -> {out}")


if __name__ == "__main__":
    main()

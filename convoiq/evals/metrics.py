"""Metric implementations from the ConvoIQ metric taxonomy (spec section 8)."""
from __future__ import annotations

import math
from collections import Counter
from typing import Iterable, Sequence

import numpy as np
from sklearn.metrics import average_precision_score, brier_score_loss, f1_score, precision_recall_fscore_support, roc_auc_score


# ---- classification / calibration -------------------------------------------------------------
def macro_micro_f1(y_true: Sequence[str], y_pred: Sequence[str]) -> dict[str, float]:
    return {"macro_f1": float(f1_score(y_true, y_pred, average="macro", zero_division=0)),
            "micro_f1": float(f1_score(y_true, y_pred, average="micro", zero_division=0)),
            "accuracy": float(np.mean(np.array(y_true) == np.array(y_pred)))}


def multilabel_f1(y_true: list[set[str]], y_pred: list[set[str]], labels: Iterable[str] | None = None) -> dict[str, float]:
    labs = sorted(set(labels) if labels else {l for s in y_true + y_pred for l in s})
    tp = fp = fn = 0
    per = {}
    for l in labs:
        a = sum(l in t and l in p for t, p in zip(y_true, y_pred))
        b = sum(l not in t and l in p for t, p in zip(y_true, y_pred))
        c = sum(l in t and l not in p for t, p in zip(y_true, y_pred))
        tp, fp, fn = tp + a, fp + b, fn + c
        per[l] = 2 * a / max(2 * a + b + c, 1) if (a + c) else float("nan")
    micro = 2 * tp / max(2 * tp + fp + fn, 1)
    vals = [v for v in per.values() if not math.isnan(v)]
    exact = float(np.mean([t == p for t, p in zip(y_true, y_pred)]))
    return {"micro_f1": micro, "macro_f1": float(np.mean(vals)) if vals else 0.0, "exact_match": exact,
            "per_label": {k: (None if math.isnan(v) else round(v, 4)) for k, v in per.items()}}


def ece(y: np.ndarray, p: np.ndarray, bins: int = 10) -> float:
    """Expected calibration error for binary probabilities."""
    y, p = np.asarray(y, float), np.asarray(p, float)
    edges = np.linspace(0, 1, bins + 1)
    tot = 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (p >= lo) & ((p < hi) if hi < 1 else (p <= hi))
        if m.any():
            tot += m.mean() * abs(y[m].mean() - p[m].mean())
    return float(tot)


def binary_report(y: Sequence[int], p: Sequence[float]) -> dict[str, float]:
    y, p = np.asarray(y), np.asarray(p)
    out = {"n": int(len(y)), "positive_rate": float(y.mean()), "brier": float(brier_score_loss(y, p)), "ece": ece(y, p)}
    if 0 < y.sum() < len(y):
        out["roc_auc"] = float(roc_auc_score(y, p))
        out["pr_auc"] = float(average_precision_score(y, p))
    return out


def lift_at_k(y: np.ndarray, p: np.ndarray, k: float = 0.1) -> float:
    n = max(1, int(len(y) * k))
    idx = np.argsort(-p)[:n]
    return float(y[idx].mean() / max(y.mean(), 1e-9))


# ---- ASR / text -------------------------------------------------------------------------------
def wer(ref: str, hyp: str) -> float:
    from convoiq.data.noise import wer as _w
    return _w(ref, hyp)


# ---- agentic ----------------------------------------------------------------------------------
def tool_selection_prf(gold: list[str], actual: list[str]) -> dict[str, float]:
    """Precision / recall of selected tools (multiset-aware), spec 8.2 TSA."""
    g, a = Counter(gold), Counter(actual)
    tp = sum((g & a).values())
    p = tp / max(sum(a.values()), 1)
    r = tp / max(sum(g.values()), 1)
    return {"precision": p, "recall": r, "f1": 2 * p * r / max(p + r, 1e-9)}


def trajectory_efficiency_ratio(optimal_steps: int, actual_steps: int) -> float:
    """TER = optimal / actual (spec 8.2). 1.0 is a perfect path; capped at 1."""
    return min(1.0, optimal_steps / actual_steps) if actual_steps else 0.0


def levenshtein(a: Sequence, b: Sequence) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def shannon_entropy(counts: Iterable[int]) -> float:
    c = np.array([x for x in counts if x > 0], float)
    if c.size == 0:
        return 0.0
    p = c / c.sum()
    return float(-(p * np.log2(p)).sum())


# ---- retrieval / RAG --------------------------------------------------------------------------
def recall_at_k(relevant: set[str], retrieved: Sequence[str], k: int) -> float:
    return len(relevant & set(retrieved[:k])) / max(len(relevant), 1)


def mrr(relevant: set[str], retrieved: Sequence[str]) -> float:
    for i, r in enumerate(retrieved, 1):
        if r in relevant:
            return 1.0 / i
    return 0.0


def faithfulness(n_attributed: int, n_total: int) -> float:
    return n_attributed / n_total if n_total else 1.0


# ---- drift ------------------------------------------------------------------------------------
def psi(expected: Sequence[float], actual: Sequence[float], eps: float = 1e-4) -> float:
    """Population Stability Index over already-binned proportions (spec 9.1.2)."""
    e = np.clip(np.asarray(expected, float), eps, None)
    a = np.clip(np.asarray(actual, float), eps, None)
    e, a = e / e.sum(), a / a.sum()
    return float(((a - e) * np.log(a / e)).sum())


def psi_continuous(expected: np.ndarray, actual: np.ndarray, bins: int = 10) -> float:
    edges = np.unique(np.quantile(expected, np.linspace(0, 1, bins + 1)))
    if len(edges) < 3:
        return 0.0
    edges[0], edges[-1] = -np.inf, np.inf
    e = np.histogram(expected, edges)[0] / len(expected)
    a = np.histogram(actual, edges)[0] / len(actual)
    return psi(e, a)


def psi_categorical(expected: dict[str, float], actual: dict[str, float]) -> float:
    keys = sorted(set(expected) | set(actual))
    return psi([expected.get(k, 0.0) for k in keys], [actual.get(k, 0.0) for k in keys])


def jsd(p: Sequence[float], q: Sequence[float]) -> float:
    """Jensen-Shannon divergence (base 2, bounded [0,1])."""
    p, q = np.asarray(p, float), np.asarray(q, float)
    p, q = p / p.sum(), q / q.sum()
    m = 0.5 * (p + q)

    def kl(a, b):
        mask = a > 0
        return float((a[mask] * np.log2(a[mask] / b[mask])).sum())

    return 0.5 * kl(p, m) + 0.5 * kl(q, m)


def ks_stat(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    from scipy.stats import ks_2samp
    r = ks_2samp(a, b)
    return float(r.statistic), float(r.pvalue)


def mmd_rbf(X: np.ndarray, Y: np.ndarray, gamma: float | None = None, max_n: int = 500, seed: int = 0) -> float:
    """Biased MMD^2 with an RBF kernel; median heuristic for gamma."""
    rng = np.random.default_rng(seed)
    if len(X) > max_n:
        X = X[rng.choice(len(X), max_n, replace=False)]
    if len(Y) > max_n:
        Y = Y[rng.choice(len(Y), max_n, replace=False)]
    Z = np.vstack([X, Y])
    if gamma is None:
        d = ((Z[:, None, :] - Z[None, :, :]) ** 2).sum(-1)
        med = np.median(d[d > 0]) if (d > 0).any() else 1.0
        gamma = 1.0 / (2 * med)

    def k(A, B):
        d = ((A[:, None, :] - B[None, :, :]) ** 2).sum(-1)
        return np.exp(-gamma * d)

    return float(k(X, X).mean() + k(Y, Y).mean() - 2 * k(X, Y).mean())


def mmd_permutation(X: np.ndarray, Y: np.ndarray, n_perm: int = 100, max_n: int = 300, seed: int = 0) -> tuple[float, float, float]:
    """Two-sample RBF-MMD with a pooled-permutation null at the SAME sample sizes as the observed comparison.

    Returns ``(observed, null_mean, p_value)``.  ``observed / null_mean`` is ~1 under H0 whatever the window size
    (biased MMD^2 scales ~1/n, so a null calibrated at a different n is wrong - this is the correct calibration).
    """
    rng = np.random.default_rng(seed)
    if len(X) > max_n:
        X = X[rng.choice(len(X), max_n, replace=False)]
    if len(Y) > max_n:
        Y = Y[rng.choice(len(Y), max_n, replace=False)]
    n, m = len(X), len(Y)
    Z = np.vstack([X, Y])
    sq = (Z * Z).sum(1)
    d = np.maximum(sq[:, None] + sq[None, :] - 2 * Z @ Z.T, 0.0)
    med = np.median(d[d > 0]) if (d > 0).any() else 1.0
    K = np.exp(-d / (2 * med))

    def stat(ix: np.ndarray, iy: np.ndarray) -> float:
        return float(K[np.ix_(ix, ix)].mean() + K[np.ix_(iy, iy)].mean() - 2 * K[np.ix_(ix, iy)].mean())

    obs = stat(np.arange(n), np.arange(n, n + m))
    nulls = np.empty(n_perm)
    for b in range(n_perm):
        perm = rng.permutation(n + m)
        nulls[b] = stat(perm[:n], perm[n:])
    p = (1 + int((nulls >= obs).sum())) / (n_perm + 1)
    return obs, float(max(nulls.mean(), 1e-9)), p


def percentile(vals: Sequence[float], q: float) -> float:
    return float(np.percentile(vals, q)) if len(vals) else 0.0

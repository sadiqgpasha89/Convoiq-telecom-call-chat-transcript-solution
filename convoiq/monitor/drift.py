"""Label-free drift monitoring (spec 9): compares a live window against training-time reference distributions.

Metrics: issue-mix PSI + JSD, sentiment KS, conversation-length PSI, embedding MMD (vs a permutation null from
the reference itself), share of low-confidence predictions, injection-attempt rate.  Nothing here needs
labels; label-based performance monitoring comes from HITL verdicts (``hitl.py``) and the nightly eval.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from convoiq.config import Settings, get_settings
from convoiq.evals.metrics import jsd, ks_stat, mmd_permutation, psi_categorical, psi_continuous

MIN_WINDOW = 50


@dataclass
class DriftMetric:
    metric: str
    value: float
    warn: float
    alert: float
    status: str            # OK | WARN | ALERT | INSUFFICIENT_DATA
    detail: str = ""

    def row(self, tenant_id: str, report_id: str, w0: datetime | None, w1: datetime | None) -> dict:
        return {"report_id": report_id, "tenant_id": tenant_id, "created_at": datetime.now(timezone.utc), "window_start": w0, "window_end": w1,
                "metric": self.metric, "value": round(self.value, 5), "threshold_warn": self.warn, "threshold_alert": self.alert,
                "status": self.status, "detail": self.detail}


def _status(v: float, warn: float, alert: float) -> str:
    return "ALERT" if v >= alert else "WARN" if v >= warn else "OK"


class DriftMonitor:
    def __init__(self, reference: dict[str, Any], settings: Settings | None = None):
        self.ref = reference
        self.s = settings or get_settings()
        self.ref_emb = np.asarray(reference.get("embedding_sample", []), dtype=float)

    @classmethod
    def from_file(cls, path: str | Path | None = None, settings: Settings | None = None) -> "DriftMonitor":
        s = settings or get_settings()
        p = Path(path or Path(s.models_dir) / "reference_distributions.json")
        return cls(json.loads(p.read_text()), s)

    def evaluate(self, rows: Sequence[dict], embeddings: np.ndarray | None = None,
                 window: tuple[datetime | None, datetime | None] = (None, None)) -> list[DriftMetric]:
        n = len(rows)
        if n < MIN_WINDOW:
            return [DriftMetric("window", float(n), MIN_WINDOW, MIN_WINDOW, "INSUFFICIENT_DATA", f"need >= {MIN_WINDOW} interactions")]
        out: list[DriftMetric] = []
        warn, alert = self.s.psi_alert, self.s.psi_retrain
        cnt: dict[str, float] = {}
        for r in rows:
            cnt[r.get("issue_type") or "OTHER"] = cnt.get(r.get("issue_type") or "OTHER", 0) + 1
        cur = {k: v / n for k, v in cnt.items()}
        refd = self.ref["issue_distribution"]
        v = psi_categorical(refd, cur)
        out.append(DriftMetric("issue_mix_psi", v, warn, alert, _status(v, warn, alert),
                               "largest shift: " + max(set(refd) | set(cur), key=lambda k: abs(cur.get(k, 0) - refd.get(k, 0)))))
        keys = sorted(set(refd) | set(cur))
        j = jsd([refd.get(k, 0.0) + 1e-9 for k in keys], [cur.get(k, 0.0) + 1e-9 for k in keys])
        out.append(DriftMetric("issue_mix_jsd", j, 0.05, 0.15, _status(j, 0.05, 0.15)))

        sm = np.array([r["sentiment_mean"] for r in rows if r.get("sentiment_mean") is not None], float)
        if len(sm) >= MIN_WINDOW:
            ref_s = np.asarray(self.ref["sentiment_mean_quantiles"], float)
            ks, pv = ks_stat(ref_s, sm)
            out.append(DriftMetric("sentiment_ks", ks, 0.15, 0.30, _status(ks, 0.15, 0.30) if pv < 0.01 else "OK", f"p={pv:.3g}"))
        nt = np.array([r["n_turns"] for r in rows if r.get("n_turns") is not None], float)
        if len(nt) >= MIN_WINDOW:
            v = psi_continuous(np.asarray(self.ref["n_turns_quantiles"], float), nt)
            out.append(DriftMetric("conversation_length_psi", v, warn, alert, _status(v, warn, alert)))

        if embeddings is not None and len(embeddings) >= MIN_WINDOW and len(self.ref_emb):
            m, null, pv = mmd_permutation(self.ref_emb, np.asarray(embeddings, float), n_perm=200, max_n=300)
            ratio = m / null
            st = _status(ratio, 3.0, 8.0) if pv < 0.01 else "OK"   # effect size gated by significance
            out.append(DriftMetric("embedding_mmd_ratio", ratio, 3.0, 8.0, st, f"mmd={m:.4f} perm-null={null:.4f} p={pv:.3g}"))

        conf = np.array([r["issue_confidence"] for r in rows if r.get("issue_confidence") is not None], float)
        if len(conf):
            low = float((conf < 0.6).mean())
            out.append(DriftMetric("low_confidence_share", low, 0.20, 0.35, _status(low, 0.20, 0.35)))
        inj = np.array([float(r.get("injection_score") or 0) >= self.s.injection_block_threshold for r in rows], float)
        out.append(DriftMetric("injection_attempt_rate", float(inj.mean()), 0.02, 0.06, _status(float(inj.mean()), 0.02, 0.06),
                               "sudden rises indicate an attack campaign, not model drift"))
        return out

    @staticmethod
    def recommendation(metrics: list[DriftMetric]) -> str:
        alerts = {m.metric for m in metrics if m.status == "ALERT"}
        warns = {m.metric for m in metrics if m.status == "WARN"}
        if {"issue_mix_psi", "embedding_mmd_ratio", "low_confidence_share"} & alerts:
            return "RETRAIN_CANDIDATE: input distribution shifted; sample 100 interactions for HITL labelling, run nightly eval on them, then retrain"
        if "injection_attempt_rate" in alerts:
            return "SECURITY_REVIEW: spike in injection attempts; tighten quarantine thresholds and review attack samples"
        if alerts or len(warns) >= 2:
            return "INVESTIGATE: multiple drift warnings"
        return "OK"


def new_report_id() -> str:
    return "drift-" + uuid.uuid4().hex[:12]

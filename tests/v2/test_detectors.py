"""Statistical monitors: outage detector (Poisson + EWMA + CUSUM), drift statistics, calibrated MMD."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from convoiq.anomaly.outage import OutageDetector, poisson_sf
from convoiq.evals.metrics import jsd, mmd_permutation, psi_categorical, psi_continuous
from convoiq.monitor import DriftMonitor

T0 = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)


def test_poisson_sf_matches_closed_form():
    assert poisson_sf(0, 3.0) == 1.0
    assert poisson_sf(1, 2.0) == pytest.approx(1 - math.exp(-2.0))
    assert poisson_sf(3, 2.0) == pytest.approx(1 - math.exp(-2) * (1 + 2 + 2))
    assert poisson_sf(20, 0.5) < 1e-15


def feed_background(d: OutageDetector, tower: str, hours: int, per_hour: int = 1):
    t = T0
    for h in range(hours):
        for k in range(per_hour):
            d.observe(tower, T0 + timedelta(hours=h, minutes=10 + 20 * k), "NO_SERVICE", f"bg-{h}-{k}")
    return T0 + timedelta(hours=hours)


def test_spike_opens_incident_and_quiet_background_does_not():
    d = OutageDetector()
    t = feed_background(d, "T1", 48, per_hour=1)
    assert not d.open_incidents(), "steady background traffic must not alarm"
    opened = None
    for k in range(12):
        opened = d.observe("T1", t + timedelta(minutes=2 * k), "NO_SERVICE", f"spike-{k}") or opened
    assert opened is not None and opened.tower_id == "T1"
    assert opened.p_value < 1e-4 and opened.count >= 6
    assert d.open_incidents() == [opened]


def test_incident_is_not_reopened_while_open_and_closes_after_quiet():
    d = OutageDetector()
    t = feed_background(d, "T1", 24)
    opens = [d.observe("T1", t + timedelta(minutes=k), "NO_SERVICE", f"s{k}") for k in range(15)]
    assert sum(o is not None for o in opens) == 1
    assert d.sweep(t + timedelta(minutes=30)) == [], "still inside the window -> stays open"
    closed = d.sweep(t + timedelta(hours=3))                     # window drained
    assert d.sweep(t + timedelta(hours=4)) or closed
    assert not d.open_incidents()


def test_towers_are_independent_and_non_network_issues_ignored():
    d = OutageDetector()
    t = feed_background(d, "T1", 24)
    for k in range(20):
        assert d.observe("T2", t + timedelta(minutes=k), "BILLING_DISPUTE", f"b{k}") is None   # not a network symptom
    assert d.observe(None, t, "NO_SERVICE", "x") is None
    for k in range(15):
        d.observe("T1", t + timedelta(minutes=k), "NO_SERVICE", f"s{k}")
    assert [i.tower_id for i in d.open_incidents()] == ["T1"]


def test_incident_cannot_teach_baseline_that_outages_are_normal():
    d = OutageDetector()
    t = feed_background(d, "T1", 24)
    base = d.baseline("T1")
    for k in range(60):
        d.observe("T1", t + timedelta(minutes=k), "NO_SERVICE", f"s{k}")     # an hour of outage traffic
    d.observe("T1", t + timedelta(hours=2), "NO_SERVICE", "after")
    assert d.baseline("T1") <= base * 1.5, "baseline must be learnt from quiet hours only"


# ---------------------------------------------------------------- drift statistics
def test_psi_and_jsd_properties():
    same = {"a": 0.5, "b": 0.5}
    assert psi_categorical(same, same) == pytest.approx(0, abs=1e-6)
    assert psi_categorical(same, {"a": 0.9, "b": 0.1}) > 0.25
    assert jsd([1, 1], [1, 1]) == pytest.approx(0, abs=1e-9)
    assert 0.99 < jsd([1, 1e-9], [1e-9, 1]) <= 1.0, "JSD base-2 is bounded by 1"
    rng = np.random.default_rng(0)
    a = rng.normal(0, 1, 2000)
    assert psi_continuous(a, rng.normal(0, 1, 2000)) < 0.05
    assert psi_continuous(a, rng.normal(1.5, 1, 2000)) > 0.25


def test_mmd_permutation_is_calibrated_under_null_and_powerful_under_shift():
    rng = np.random.default_rng(1)
    ref = rng.normal(size=(400, 8))
    ratios = [mmd_permutation(ref, rng.normal(size=(n, 8)), n_perm=100)[0] / mmd_permutation(ref, rng.normal(size=(n, 8)), n_perm=100)[1]
              for n in (80, 400)]
    # ratio ~1 under H0 whatever the window size (the old split-half null was off by the n-ratio)
    assert all(r < 3 for r in ratios), ratios
    obs, null, p = mmd_permutation(ref, rng.normal(0.6, 1, size=(300, 8)), n_perm=200)
    assert obs / null > 5 and p < 0.01


def test_pvalue_resolution_allows_alerting():
    rng = np.random.default_rng(2)
    _, _, p = mmd_permutation(rng.normal(size=(200, 4)), rng.normal(3, 1, size=(200, 4)), n_perm=200)
    assert p < 0.01, "need >=200 permutations so the minimum p-value (1/201) can clear a 0.01 gate"


def _ref(n=400, d=6, seed=0):
    rng = np.random.default_rng(seed)
    return {"issue_distribution": {"NO_SERVICE": 0.5, "BILLING_DISPUTE": 0.5},
            "sentiment_mean_quantiles": np.quantile(rng.normal(0, 0.3, 1000), np.linspace(0, 1, 21)).tolist(),
            "n_turns_quantiles": np.quantile(rng.integers(4, 20, 1000), np.linspace(0, 1, 21)).tolist(),
            "embedding_sample": rng.normal(size=(n, d)).tolist()}


def _rows(rng, n, p_no_service=0.5, sent_mu=0.0):
    return [{"issue_type": "NO_SERVICE" if rng.random() < p_no_service else "BILLING_DISPUTE", "sentiment_mean": float(rng.normal(sent_mu, 0.3)),
             "n_turns": int(rng.integers(4, 20)), "resolution_risk": 0.3, "injection_score": 0.0, "issue_confidence": 0.9} for _ in range(n)]


def test_drift_monitor_quiet_on_clean_alerts_on_shift(settings):
    dm = DriftMonitor(_ref(), settings)
    rng = np.random.default_rng(5)
    clean = dm.evaluate(_rows(rng, 300), rng.normal(size=(300, 6)))
    assert [m.metric for m in clean if m.status == "ALERT"] == []
    shifted = dm.evaluate(_rows(rng, 300, p_no_service=0.97), rng.normal(1.0, 1, size=(300, 6)))
    alerts = {m.metric for m in shifted if m.status == "ALERT"}
    assert {"issue_mix_psi", "embedding_mmd_ratio"} <= alerts
    assert dm.recommendation(shifted).startswith("RETRAIN")


def test_drift_monitor_refuses_tiny_windows(settings):
    dm = DriftMonitor(_ref(), settings)
    rng = np.random.default_rng(6)
    out = dm.evaluate(_rows(rng, 10), rng.normal(size=(10, 6)))
    assert [m.status for m in out] == ["INSUFFICIENT_DATA"]

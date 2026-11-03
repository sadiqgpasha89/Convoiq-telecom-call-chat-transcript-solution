"""Network-incident detection: delay, recall and false-alarm rate on synthetic streams with an injected tower outage."""
from __future__ import annotations

from datetime import datetime

import numpy as np

from convoiq.anomaly import OutageDetector
from convoiq.data.synth import generate_outage_stream
from convoiq.evals.context import EvalContext


def _replay(convs: list[dict]) -> list:
    ev = sorted(((datetime.fromisoformat(c["meta"]["started_at"]), c) for c in convs), key=lambda x: x[0])
    d = OutageDetector()
    found = []
    for ts, c in ev:
        inc = d.observe(c["meta"]["cell_tower_id"], ts, c["issue_type"], c["conv_id"], c["meta"].get("region"))
        if inc:
            found.append(inc)
    return found


def run(ctx: EvalContext) -> dict:
    delays, fa, detected = [], [], []
    for seed in range(6):
        convs, truth = generate_outage_stream(seed=seed, hours=72, outage_start_h=40, outage_len_h=6, outage_tower=f"TWR-BOS-{seed:03d}9")
        found = _replay(convs)
        start = datetime.fromisoformat(truth["start"])
        hit = [i for i in found if i.tower_id == truth["tower"]]
        detected.append(bool(hit))
        if hit:
            delays.append((hit[0].opened_at - start).total_seconds() / 60)
        fa.append(len([i for i in found if i.tower_id != truth["tower"]]))
    # control: no outage at all, 7 days
    ctrl_alarms = []
    for seed in range(3):
        convs, _ = generate_outage_stream(seed=100 + seed, hours=168, outage_len_h=0)
        ctrl_alarms.append(len(_replay(convs)))
    return {"outage_detection_rate": float(np.mean(detected)), "detection_delay_minutes_median": float(np.median(delays)) if delays else None,
            "detection_delay_minutes_max": float(max(delays)) if delays else None, "false_alarms_per_72h_with_outage": float(np.mean(fa)),
            "control_false_alarms_per_7_days": float(np.mean(ctrl_alarms)), "streams": 6}

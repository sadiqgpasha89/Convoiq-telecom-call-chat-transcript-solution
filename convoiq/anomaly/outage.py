"""Streaming network-incident detector (spec: tower-level complaint spikes -> OSS ticket before customers churn).

Per cell-site we keep a sliding 60-minute window of *network-type* complaints and an EWMA of the hourly
baseline rate (updated only from quiet hours, so an outage cannot teach the detector that outages are normal).
An incident opens when the window count is improbable under Poisson(baseline) (p < alpha) AND at least
``min_count`` events exist; a CUSUM on the same counts provides a second, slower trigger for sustained
mild elevations.  An incident closes when the window count falls back to <= 2x baseline for 30 minutes.

Complexity O(1) amortised per event; state per tower is a deque + 3 floats -> trivially shardable by tower id.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

NETWORK_ISSUES = {"NO_SERVICE", "MOBILE_DATA"}
WINDOW = timedelta(minutes=60)
CLOSE_AFTER = timedelta(minutes=30)


def poisson_sf(k: int, lam: float) -> float:
    """P(X >= k) for X ~ Poisson(lam) (exact, stable for the small k we need)."""
    if k <= 0:
        return 1.0
    lam = max(lam, 1e-9)
    term = math.exp(-lam)
    cdf = term
    for i in range(1, k):
        term *= lam / i
        cdf += term
    return max(0.0, 1.0 - cdf)


@dataclass
class Incident:
    incident_id: str
    tower_id: str
    region: str | None
    opened_at: datetime
    window_start: datetime
    last_event_at: datetime
    count: int
    expected_rate: float
    p_value: float
    zscore: float
    status: str = "OPEN"
    interaction_ids: list[str] = field(default_factory=list)
    issue_mix: dict[str, int] = field(default_factory=dict)
    ticket_ref: str | None = None
    trigger: str = "poisson"

    def as_row(self, tenant_id: str = "default") -> dict:
        return {"incident_id": self.incident_id, "tenant_id": tenant_id, "detected_at": self.opened_at, "cell_tower_id": self.tower_id,
                "region": self.region, "issue_type": max(self.issue_mix, key=self.issue_mix.get) if self.issue_mix else None,
                "window_start": self.window_start, "window_end": self.last_event_at, "n_interactions": self.count,
                "expected_rate": round(self.expected_rate, 3), "observed_rate": float(self.count), "zscore": round(self.zscore, 2),
                "status": self.status, "ticket_ref": self.ticket_ref}


@dataclass
class _Tower:
    events: deque = field(default_factory=deque)                 # (ts, interaction_id, issue)
    rate: float | None = None                                    # EWMA events/hour (quiet hours only)
    hour_start: datetime | None = None
    hour_count: int = 0
    hour_quiet: bool = True
    cusum: float = 0.0
    incident: Incident | None = None
    quiet_since: datetime | None = None
    n_hours_seen: int = 0


class OutageDetector:
    def __init__(self, alpha: float = 1e-4, min_count: int = 6, ewma: float = 0.2, prior_rate: float = 0.6,
                 cusum_k: float = 0.5, cusum_h: float = 8.0, warmup_hours: int = 0):
        self.alpha, self.min_count, self.ew, self.prior = alpha, min_count, ewma, prior_rate
        self.k, self.h, self.warmup = cusum_k, cusum_h, warmup_hours
        self.towers: dict[str, _Tower] = {}
        self.closed: list[Incident] = []
        self._seq = 0

    # -- helpers -------------------------------------------------------------------------------------
    def _roll_hour(self, tw: _Tower, ts: datetime) -> None:
        hs = ts.replace(minute=0, second=0, microsecond=0)
        if tw.hour_start is None:
            tw.hour_start = hs
            return
        while tw.hour_start < hs:
            if tw.hour_quiet:      # learn the baseline only from hours with no open/suspected incident
                tw.rate = tw.hour_count if tw.rate is None else (1 - self.ew) * tw.rate + self.ew * tw.hour_count
                tw.n_hours_seen += 1
            tw.cusum = max(0.0, tw.cusum + (tw.hour_count - (tw.rate or self.prior)) - self.k * math.sqrt(max(tw.rate or self.prior, 0.1)))
            tw.hour_start += timedelta(hours=1)
            tw.hour_count, tw.hour_quiet = 0, tw.incident is None
            if tw.hour_start < hs:
                tw.hour_count = 0

    def baseline(self, tower_id: str) -> float:
        tw = self.towers.get(tower_id)
        return max((tw.rate if tw and tw.rate is not None else self.prior), 0.2)

    # -- main API ---------------------------------------------------------------------------------------
    def observe(self, tower_id: str | None, ts: datetime, issue_type: str, interaction_id: str, region: str | None = None) -> Incident | None:
        """Feed one interaction. Returns an Incident when one is *newly opened*."""
        if not tower_id or issue_type not in NETWORK_ISSUES:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        tw = self.towers.setdefault(tower_id, _Tower())
        self._roll_hour(tw, ts)
        tw.hour_count += 1
        tw.events.append((ts, interaction_id, issue_type))
        while tw.events and ts - tw.events[0][0] > WINDOW:
            tw.events.popleft()
        n = len(tw.events)
        lam = self.baseline(tower_id)
        p = poisson_sf(n, lam)
        z = (n - lam) / math.sqrt(lam)
        inc = tw.incident
        if inc is not None:
            inc.count, inc.last_event_at = max(inc.count, n), ts
            if interaction_id not in inc.interaction_ids and len(inc.interaction_ids) < 200:
                inc.interaction_ids.append(interaction_id)
            inc.issue_mix[issue_type] = inc.issue_mix.get(issue_type, 0) + 1
            tw.quiet_since = None
            return None
        if tw.n_hours_seen < self.warmup:
            return None
        trig = None
        if n >= self.min_count and p < self.alpha:
            trig = "poisson"
        elif tw.cusum > self.h and n >= self.min_count:
            trig = "cusum"
        if trig:
            self._seq += 1
            tw.hour_quiet = False
            inc = Incident(incident_id=f"inc-{tower_id}-{int(tw.events[0][0].timestamp())}", tower_id=tower_id, region=region,
                           opened_at=ts, window_start=tw.events[0][0], last_event_at=ts, count=n, expected_rate=lam, p_value=p,
                           zscore=z, interaction_ids=[e[1] for e in tw.events][:200], trigger=trig)
            for _, _, it in tw.events:
                inc.issue_mix[it] = inc.issue_mix.get(it, 0) + 1
            tw.incident = inc
            return inc
        return None

    def sweep(self, now: datetime) -> list[Incident]:
        """Close incidents that have been quiet; call periodically (Cloud Scheduler -> /sweep)."""
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        out = []
        for tid, tw in self.towers.items():
            while tw.events and now - tw.events[0][0] > WINDOW:
                tw.events.popleft()
            inc = tw.incident
            if inc is None:
                continue
            if len(tw.events) <= 2 * self.baseline(tid):
                tw.quiet_since = tw.quiet_since or now
                if now - tw.quiet_since >= CLOSE_AFTER:
                    inc.status = "RESOLVED"
                    tw.incident, tw.quiet_since, tw.cusum = None, None, 0.0
                    self.closed.append(inc)
                    out.append(inc)
            else:
                tw.quiet_since = None
        return out

    def open_incidents(self) -> list[Incident]:
        return [tw.incident for tw in self.towers.values() if tw.incident]

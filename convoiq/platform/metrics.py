"""Minimal thread-safe metrics registry with Prometheus text exposition (no dependencies).

On GCP, Cloud Run sidecar / Managed Service for Prometheus scrapes ``/metrics``; histograms use fixed
buckets so P95/P99 can be derived for the latency SLOs in docs/SLOs.md.
"""
from __future__ import annotations

import threading
from typing import Iterable

DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)


def _k(labels: dict[str, str]) -> tuple:
    return tuple(sorted(labels.items()))


class _Metric:
    def __init__(self, name: str, help: str, labelnames: Iterable[str]):
        self.name, self.help, self.labelnames = name, help, tuple(labelnames)
        self._lock = threading.Lock()


class Counter(_Metric):
    kind = "counter"

    def __init__(self, *a):
        super().__init__(*a)
        self._v: dict[tuple, float] = {}

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        with self._lock:
            self._v[_k(labels)] = self._v.get(_k(labels), 0.0) + amount

    def value(self, **labels: str) -> float:
        return self._v.get(_k(labels), 0.0)

    def render(self) -> list[str]:
        return [f"{self.name}{_fmt(dict(k))} {v}" for k, v in sorted(self._v.items())]


class Gauge(Counter):
    kind = "gauge"

    def set(self, value: float, **labels: str) -> None:
        with self._lock:
            self._v[_k(labels)] = value


class Histogram(_Metric):
    kind = "histogram"

    def __init__(self, name, help, labelnames, buckets=DEFAULT_BUCKETS):
        super().__init__(name, help, labelnames)
        self.buckets = tuple(buckets)
        self._d: dict[tuple, list] = {}

    def observe(self, value: float, **labels: str) -> None:
        with self._lock:
            d = self._d.setdefault(_k(labels), [[0] * len(self.buckets), 0, 0.0])
            for i, b in enumerate(self.buckets):
                if value <= b:
                    d[0][i] += 1
            d[1] += 1
            d[2] += value

    def quantile(self, q: float, **labels: str) -> float:
        d = self._d.get(_k(labels))
        if not d or d[1] == 0:
            return 0.0
        target = q * d[1]
        for b, c in zip(self.buckets, d[0]):
            if c >= target:
                return b
        return float("inf")

    def render(self) -> list[str]:
        out = []
        for k, (counts, n, s) in sorted(self._d.items()):
            lab = dict(k)
            for b, c in zip(self.buckets, counts):
                out.append(f"{self.name}_bucket{_fmt({**lab, 'le': str(b)})} {c}")
            out.append(f"{self.name}_bucket{_fmt({**lab, 'le': '+Inf'})} {n}")
            out.append(f"{self.name}_sum{_fmt(lab)} {s}")
            out.append(f"{self.name}_count{_fmt(lab)} {n}")
        return out


def _fmt(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{str(v)}"' for k, v in sorted(labels.items())) + "}"


class Registry:
    def __init__(self):
        self._m: dict[str, _Metric] = {}
        self._lock = threading.Lock()

    def _get(self, cls, name, help, labels, **kw):
        with self._lock:
            if name not in self._m:
                self._m[name] = cls(name, help, labels, **kw) if cls is Histogram else cls(name, help, labels)
            return self._m[name]

    def counter(self, name: str, help: str = "", labels: Iterable[str] = ()) -> Counter:
        return self._get(Counter, name, help, labels)

    def gauge(self, name: str, help: str = "", labels: Iterable[str] = ()) -> Gauge:
        return self._get(Gauge, name, help, labels)

    def histogram(self, name: str, help: str = "", labels: Iterable[str] = (), buckets=DEFAULT_BUCKETS) -> Histogram:
        return self._get(Histogram, name, help, labels, buckets=buckets)

    def render(self) -> str:
        lines = []
        for m in self._m.values():
            lines.append(f"# HELP {m.name} {m.help}")
            lines.append(f"# TYPE {m.name} {m.kind}")
            lines.extend(m.render())
        return "\n".join(lines) + "\n"


REGISTRY = Registry()

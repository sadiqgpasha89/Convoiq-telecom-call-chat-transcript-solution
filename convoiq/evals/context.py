"""Shared, lazily-built fixtures for eval suites (one model load, one dataset build per run)."""
from __future__ import annotations

import gzip
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from convoiq.agents.tools import InMemoryTelecomTools
from convoiq.config import ROOT, Settings, set_settings
from convoiq.data.seed import seed_inmemory
from convoiq.data.synth import generate
from convoiq.data.tau2_corpus import remediation_map
from convoiq.models.bundle import ModelBundle
from convoiq.pipeline import LocalPipeline
from convoiq.training.datasets import load_tau2, prep, split_tau2, tau2_split


@dataclass
class EvalContext:
    models_dir: str
    n_syn: int = 600
    n_tau2_test: int | None = None
    seed: int = 777
    settings: Settings = field(default_factory=lambda: Settings(env="local", auth_mode="none"))
    _cache: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.settings = self.settings.model_copy(update={"models_dir": self.models_dir})
        set_settings(self.settings)

    # ---- models / pipeline ---------------------------------------------------------------------------------------
    @property
    def bundle(self) -> ModelBundle:
        if "bundle" not in self._cache:
            b = ModelBundle.load(self.settings, self.models_dir)
            if b.missing:
                raise RuntimeError(f"eval models missing: {b.missing}; run `make train-eval`")
            self._cache["bundle"] = b
            mf = Path(self.models_dir) / "MANIFEST.json"
            self._cache["model_split"] = json.loads(mf.read_text()).get("split", "unknown") if mf.exists() else "unknown"
        return self._cache["bundle"]

    @property
    def tools(self) -> InMemoryTelecomTools:
        if "tools" not in self._cache:
            self._cache["tools"] = InMemoryTelecomTools()
        return self._cache["tools"]

    @property
    def pipeline(self) -> LocalPipeline:
        if "pipeline" not in self._cache:
            p = LocalPipeline(self.settings, self.bundle, tools=self.tools)
            p.deps.remediation = self.train_remediation          # never use gold from test tasks
            self._cache["pipeline"] = p
        return self._cache["pipeline"]

    # ---- data ------------------------------------------------------------------------------------------------------
    @property
    def tau2_test(self) -> list[dict]:
        if "tau2_test" not in self._cache:
            convs = split_tau2(load_tau2(), "test")
            # one trial per (task, model, mode) is already unique; cap for speed if requested
            self._cache["tau2_test"] = convs[: self.n_tau2_test] if self.n_tau2_test else convs
        return self._cache["tau2_test"]

    @property
    def train_remediation(self) -> dict[str, list[str]]:
        if "rem" not in self._cache:
            tr = tau2_split()["train"]
            p = ROOT / "data" / "corpus" / "tau2_telecom_tickets.jsonl.gz"
            tickets = [json.loads(l) for l in gzip.open(p, "rt")]
            self._cache["rem"] = remediation_map([t for t in tickets if t["ticket_id"] in tr])
        return self._cache["rem"]

    def synth(self, n: int | None = None, seed: int | None = None, noise: float = 0.0, families: list[str] | None = None, seeded: bool = True) -> list[dict]:
        key = f"synth:{n}:{seed}:{noise}:{families}"
        if key not in self._cache:
            convs = generate(n or self.n_syn, seed=seed if seed is not None else self.seed, noise_rate=noise, families=families)
            if seeded:
                for c in convs:
                    seed_inmemory(self.tools, c, self.pipeline.redactor.tok)
            self._cache[key] = convs
        return self._cache[key]

    @staticmethod
    def turns(conv: dict):
        return prep(conv)

    def timer(self):
        return time.perf_counter()

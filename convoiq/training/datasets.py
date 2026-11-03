"""Dataset assembly for training + evaluation (deterministic)."""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

from convoiq.config import ROOT
from convoiq.data.synth import generate
from convoiq.nlp.text import corpus_turns_to_models

CORPUS = ROOT / "data" / "corpus" / "tau2_telecom_conversations.jsonl.gz"
SAMPLE = ROOT / "data" / "samples" / "tau2_conversations_sample.jsonl"
SPLIT = ROOT / "data" / "reference" / "tau2_split_tasks.json"


def load_tau2(path: Path | None = None) -> list[dict]:
    p = path or (CORPUS if CORPUS.exists() else SAMPLE)
    opener = gzip.open if str(p).endswith(".gz") else open
    with opener(p, "rt", encoding="utf-8") as f:  # type: ignore[arg-type]
        return [json.loads(l) for l in f]


def tau2_split() -> dict[str, set[str]]:
    d = json.loads(SPLIT.read_text())
    return {k: set(v) for k, v in d.items()}


def split_tau2(convs: list[dict], which: str) -> list[dict]:
    """Official tau2 task split: train (74 tasks) / test (40 tasks). which in {'train','test','all'}."""
    if which == "all":
        return convs
    ids = tau2_split()[which]
    return [c for c in convs if c["task_id"] in ids]


def data_hash(*paths: Path) -> str:
    h = hashlib.sha256()
    for p in paths:
        if p.exists():
            h.update(p.read_bytes())
    return h.hexdigest()


def prep(conv: dict):
    """-> (turns:list[Turn]) cached on the dict to avoid repeated pydantic work."""
    t = conv.get("_turns")
    if t is None:
        t = corpus_turns_to_models(conv["turns"])
        conv["_turns"] = t
    return t


def synthetic(n: int, seed: int, noise_rate: float = 0.0) -> list[dict]:
    return generate(n, seed=seed, noise_rate=noise_rate)

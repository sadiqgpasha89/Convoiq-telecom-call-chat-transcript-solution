"""Case-based reasoning: nearest *resolved* historical conversations and the fixes that worked."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import numpy as np

from convoiq.models.classical import LsaEmbedder


@dataclass
class SimilarCase:
    conv_id: str
    task_id: str
    similarity: float
    root_causes: list[str]
    fixes: list[str]


class CaseIndex:
    def __init__(self, vectors: np.ndarray, cases: list[dict], embedder: LsaEmbedder):
        self.V, self.cases, self.embedder = vectors, cases, embedder

    def search(self, query_text: str, k: int = 5, issue_type: str | None = None) -> list[SimilarCase]:
        q = self.embedder.embed_texts([query_text])[0]
        sims = self.V @ q
        order = np.argsort(-sims)
        out = []
        for i in order:
            c = self.cases[int(i)]
            if issue_type and c["issue_type"] != issue_type:
                continue
            out.append(SimilarCase(c["conv_id"], c["task_id"], float(sims[i]), c["root_causes"], c["fixes"]))
            if len(out) >= k:
                break
        return out

    @staticmethod
    def vote_fixes(cases: list[SimilarCase], min_share: float = 0.4) -> list[tuple[str, float]]:
        if not cases:
            return []
        c = Counter(f for sc in cases for f in set(sc.fixes))
        n = len(cases)
        return [(f, v / n) for f, v in c.most_common() if v / n >= min_share]

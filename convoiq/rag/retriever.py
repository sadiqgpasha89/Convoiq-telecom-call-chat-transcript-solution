"""Hybrid retrieval: BM25 (lexical) + LSA dense similarity, fused with Reciprocal Rank Fusion."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from convoiq.models.classical import LsaEmbedder
from convoiq.rag.kb import BM25, Chunk, load_chunks, tokenize


@dataclass
class Hit:
    chunk: Chunk
    score: float
    lexical_rank: int | None = None
    dense_rank: int | None = None


class HybridRetriever:
    def __init__(self, chunks: list[Chunk] | None = None, embedder: LsaEmbedder | None = None, rrf_k: int = 60):
        self.chunks = chunks if chunks is not None else load_chunks()
        self.by_id = {c.id: c for c in self.chunks}
        self.bm25 = BM25([tokenize(c.text) for c in self.chunks])
        self.embedder = embedder
        self.rrf_k = rrf_k
        self.dense = embedder.embed_texts([c.text for c in self.chunks]) if embedder is not None else None

    @property
    def ids(self) -> set[str]:
        return set(self.by_id)

    def retrieve(self, query: str, k: int = 4, mode: str = "hybrid") -> list[Hit]:
        lex = self.bm25.scores(tokenize(query))
        lex_order = np.argsort(-lex)
        lex_rank = {int(i): r for r, i in enumerate(lex_order) if lex[i] > 0}
        dense_rank: dict[int, int] = {}
        if self.dense is not None and mode in ("hybrid", "dense"):
            q = self.embedder.embed_texts([query])[0]
            sims = self.dense @ q
            dense_rank = {int(i): r for r, i in enumerate(np.argsort(-sims))}
        scores: dict[int, float] = {}
        if mode in ("hybrid", "lexical"):
            for i, r in lex_rank.items():
                scores[i] = scores.get(i, 0.0) + 1.0 / (self.rrf_k + r + 1)
        for i, r in dense_rank.items():
            if r < 20:
                scores[i] = scores.get(i, 0.0) + 1.0 / (self.rrf_k + r + 1)
        top = sorted(scores, key=lambda i: -scores[i])[:k]
        return [Hit(self.chunks[i], scores[i], lex_rank.get(i), dense_rank.get(i)) for i in top]

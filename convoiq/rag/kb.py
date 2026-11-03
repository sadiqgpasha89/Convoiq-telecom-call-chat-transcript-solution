"""Knowledge base: tau2 telecom policy + tech-support manual + workflow, chunked with stable ids."""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from convoiq.config import get_settings

_TOK = re.compile(r"[a-z0-9_]+")
STOP = set("the a an and or of to in on for is are be it this that with as at by from you your can if not will".split())


@dataclass
class Chunk:
    id: str
    doc: str
    heading: str
    text: str


def tokenize(text: str) -> list[str]:
    return [t for t in _TOK.findall(text.lower()) if t not in STOP and len(t) > 1]


def chunk_markdown(doc: str, text: str, max_chars: int = 900) -> list[Chunk]:
    """Split on headings, then pack paragraphs up to ``max_chars``; keep heading path for context."""
    chunks: list[Chunk] = []
    path: list[str] = []
    buf: list[str] = []
    n = 0

    def flush():
        nonlocal buf, n
        body = "\n".join(buf).strip()
        if body:
            heading = " > ".join(path)
            chunks.append(Chunk(f"kb:{doc}#{n}", doc, heading, (heading + "\n" + body) if heading else body))
            n += 1
        buf = []

    for line in text.splitlines():
        m = re.match(r"^(#{1,4})\s+(.*)", line)
        if m:
            flush()
            lvl = len(m.group(1))
            path[:] = path[: lvl - 1] + [m.group(2).strip()]
            continue
        buf.append(line)
        if sum(len(x) for x in buf) > max_chars and not line.strip():
            flush()
    flush()
    return chunks


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.4, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs = docs
        self.avg = sum(map(len, docs)) / max(len(docs), 1)
        df: Counter = Counter()
        for d in docs:
            df.update(set(d))
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.tf = [Counter(d) for d in docs]

    def scores(self, q: list[str]) -> np.ndarray:
        out = np.zeros(len(self.docs))
        for i, (tf, d) in enumerate(zip(self.tf, self.docs)):
            s = 0.0
            for t in q:
                f = tf.get(t, 0)
                if f:
                    s += self.idf.get(t, 0) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * len(d) / self.avg))
            out[i] = s
        return out


def load_chunks(kb_dir: str | Path | None = None) -> list[Chunk]:
    d = Path(kb_dir or get_settings().kb_dir)
    chunks: list[Chunk] = []
    for p in sorted(d.glob("*.md")):
        chunks.extend(chunk_markdown(p.stem, p.read_text()))
    return chunks

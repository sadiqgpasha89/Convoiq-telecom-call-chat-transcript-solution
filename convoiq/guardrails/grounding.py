"""Claim grounding / hallucination control (spec 12.2: every claim carries sentence-level citations).

A claim is *attributed* only if (a) it cites at least one evidence ref and (b) every ref resolves to a real
object in this interaction's evidence universe (turn ids, KB chunk ids, record ids, model outputs), and
(c) any quoted text is a substring of the cited turn.  Unattributed claims are dropped from the final
summary; the share of attributed claims is the **faithfulness** metric.
"""
from __future__ import annotations

import re

from convoiq.contracts import Claim, Evidence, Turn

_ws = re.compile(r"\s+")


def _n(s: str) -> str:
    return _ws.sub(" ", s.lower()).strip()


class EvidenceUniverse:
    def __init__(self, turns: list[Turn], kb_ids: set[str] | None = None, record_ids: set[str] | None = None,
                 model_ids: set[str] | None = None):
        self.turns = {t.i: t for t in turns}
        self.kb_ids = kb_ids or set()
        self.record_ids = record_ids or set()
        self.model_ids = model_ids or set()

    def resolve(self, ev: Evidence) -> bool:
        kind, ref = ev.kind, ev.ref
        if kind == "turn":
            m = re.fullmatch(r"turn:(\d+)", ref)
            if not m or int(m.group(1)) not in self.turns:
                return False
            if ev.quote:
                t = self.turns[int(m.group(1))]
                hay = t.text + " " + " ".join(f"{tc.name} {tc.arguments}" for tc in t.tool_calls)
                return _n(ev.quote) in _n(hay)
            return True
        if kind == "kb":
            return ref in self.kb_ids
        if kind == "record" or kind == "tool":
            return ref in self.record_ids
        if kind == "model":
            name = ref.split(":", 1)[-1]
            return name.split("@")[0] in self.model_ids or name in self.model_ids
        return False


def verify_claims(claims: list[Claim], universe: EvidenceUniverse) -> tuple[list[Claim], float]:
    out, ok = [], 0
    for c in claims:
        valid = bool(c.evidence) and all(universe.resolve(e) for e in c.evidence)
        out.append(c.model_copy(update={"attributed": valid}))
        ok += valid
    faith = ok / len(claims) if claims else 1.0
    return out, faith

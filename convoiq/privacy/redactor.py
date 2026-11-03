"""Redaction engine: RawInteraction -> RedactedInteraction (+ attestation).

Runs in the sovereign/edge zone (OpenStack or a locked-down GCP project).  Only the output of this
class is allowed to cross into the analytics plane.  Dual-pass design (spec 12.1, PCI-DSS):

1. **Pass 1** - span detection with conversation cues, replaced by HMAC tokens.
2. **Pass 2** - ``residual_scan``: re-detects on the redacted text in strict mode (every cue on); any
   finding is counted in the attestation and the cloud side quarantines non-zero payloads.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from convoiq.contracts import RawInteraction, RedactedInteraction, RedactionAttestation, Speaker, Turn
from convoiq.privacy.detectors import Cue, Detector, NameDetector, Span, default_detectors, detect_all
from convoiq.privacy.tokenizer import TOKEN_RE, Tokenizer
from convoiq.privacy.vault import TokenVault

REDACTOR_VERSION = "1.1.0"
STRICT_CUE = Cue(pin=True, dob=True, phone=True, card=True, ssn=True, account=True, name=False)


@dataclass
class TextResult:
    text: str
    spans: list[Span]
    tokens: list[str] = field(default_factory=list)


def _mask_existing_tokens(text: str) -> str:
    return TOKEN_RE.sub(lambda m: "_" * len(m.group(0)), text)


class Redactor:
    def __init__(self, tokenizer: Tokenizer, vault: TokenVault | None = None,
                 extra_detectors: list[Detector] | None = None, token_types: set[str] | None = None):
        self.tok = tokenizer
        self.vault = vault
        self.extra = extra_detectors or []
        self.token_types = token_types  # None = redact every detected type

    # ---- text level ------------------------------------------------------------------------
    def _detectors(self, known_names: list[str]) -> list[Detector]:
        return default_detectors(known_names) + self.extra

    def redact_text(self, text: str, cue: Cue | None = None, known_names: list[str] | None = None,
                    subject: str | None = None) -> TextResult:
        if not text:
            return TextResult(text, [])
        masked = _mask_existing_tokens(text)
        spans = detect_all(masked, self._detectors(known_names or []), cue)
        if self.token_types:
            spans = [s for s in spans if s.type in self.token_types]
        out, tokens = text, []
        for s in sorted(spans, key=lambda s: s.start, reverse=True):
            token = self.tok.token(s.type, text[s.start:s.end])
            if self.vault is not None:
                self.vault.put(token, s.type, text[s.start:s.end], subject)
            out = out[: s.start] + token + out[s.end:]
            tokens.append(token)
        return TextResult(out, spans, tokens[::-1])

    def residual_scan(self, text: str, known_names: list[str] | None = None) -> list[Span]:
        """Strict second pass over already-redacted text (tokens masked first)."""
        masked = _mask_existing_tokens(text)
        spans = detect_all(masked, self._detectors(known_names or []), STRICT_CUE)
        # strict mode ignores the weakest heuristics to keep the alarm meaningful
        return [s for s in spans if s.source not in ("gazetteer",) and not (s.type == "NAME" and s.score < 0.8)]

    # ---- structured payloads ----------------------------------------------------------------------
    def _redact_obj(self, obj: Any, known: list[str], counts: dict[str, int], subject: str | None) -> Any:
        if isinstance(obj, str):
            r = self.redact_text(obj, known_names=known, subject=subject)
            for s in r.spans:
                counts[s.type] = counts.get(s.type, 0) + 1
            return r.text
        if isinstance(obj, dict):
            return {k: self._redact_obj(v, known, counts, subject) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._redact_obj(v, known, counts, subject) for v in obj]
        return obj

    def redact_interaction(self, raw: RawInteraction) -> RedactedInteraction:
        subject = raw.meta.msisdn or raw.meta.customer_ref
        pseudo = self.tok.pseudo_id(subject) if subject else None
        counts: dict[str, int] = {}
        known: list[str] = []
        turns_out: list[Turn] = []
        prev_cue = Cue()
        for t in raw.turns:
            cue = prev_cue
            res = self.redact_text(t.text, cue=cue, known_names=known, subject=pseudo)
            for s in res.spans:
                counts[s.type] = counts.get(s.type, 0) + 1
                if s.type == "NAME":
                    parts = [p for p in re.split(r"\s+", s.value) if len(p) > 2]
                    known.extend([s.value] + parts)
            nt = t.model_copy(deep=True)
            nt.text = res.text
            if t.tool_calls:
                nt.tool_calls = [tc.model_copy(update={"arguments": self._redact_obj(tc.arguments, known, counts, pseudo)})
                                 for tc in t.tool_calls]
            turns_out.append(nt)
            # an agent question primes the next customer answer
            if t.role in (Speaker.AGENT, Speaker.BOT):
                prev_cue = Cue.from_agent_text(t.text)
            elif t.role == Speaker.CUSTOMER:
                prev_cue = Cue()
        meta = raw.meta.model_copy(deep=True)
        meta.customer_ref = None
        meta.msisdn = None
        if meta.agent_id:
            meta.agent_id = self.tok.pseudo_id(meta.agent_id, "csr")
        meta.extra = {k: v for k, v in meta.extra.items() if not isinstance(v, str)}

        residual = sum(len(self.residual_scan(t.text, known)) for t in turns_out if t.text)
        digest = hashlib.sha256(json.dumps([t.model_dump(mode="json") for t in turns_out], sort_keys=True).encode()).hexdigest()
        att = RedactionAttestation(version=REDACTOR_VERSION, content_sha256=digest, counts=counts,
                                   residual_findings=residual, zone="edge")
        att.signature = self.tok.sign(digest, raw.interaction_id)
        return RedactedInteraction(interaction_id=raw.interaction_id, tenant_id=raw.tenant_id, channel=raw.channel,
                                   customer_pseudo_id=pseudo, turns=turns_out, meta=meta, attestation=att,
                                   received_at=raw.received_at)


def second_pass(redactor: "Redactor", red: RedactedInteraction) -> RedactedInteraction:
    """Dual-pass safety net: re-redact an already-redacted payload with the strict cue; recompute the attestation.

    Over-redaction of stray digit strings is the accepted price for never forwarding a residual identifier.
    """
    counts = dict(red.attestation.counts)
    turns = []
    for t in red.turns:
        nt = t.model_copy(deep=True)
        if t.text:
            r = redactor.redact_text(t.text, cue=STRICT_CUE, subject=red.customer_pseudo_id)
            nt.text = r.text
            for sp in r.spans:
                counts[sp.type] = counts.get(sp.type, 0) + 1
        turns.append(nt)
    residual = sum(len(redactor.residual_scan(t.text)) for t in turns if t.text)
    digest = hashlib.sha256(json.dumps([t.model_dump(mode="json") for t in turns], sort_keys=True).encode()).hexdigest()
    att = red.attestation.model_copy(update={"content_sha256": digest, "counts": counts, "residual_findings": residual})
    att.signature = redactor.tok.sign(digest, red.interaction_id)
    return red.model_copy(update={"turns": turns, "attestation": att})


def verify_attestation(red: RedactedInteraction, tokenizer: Tokenizer | None, redactor_for_scan: Redactor | None = None,
                       require_signature: bool = False) -> list[str]:
    """Cloud-side defence in depth. Returns a list of problems (empty = accept)."""
    problems: list[str] = []
    digest = hashlib.sha256(json.dumps([t.model_dump(mode="json") for t in red.turns], sort_keys=True).encode()).hexdigest()
    if digest != red.attestation.content_sha256:
        problems.append("content hash mismatch (payload modified after redaction)")
    if require_signature:
        if tokenizer is None or not tokenizer.verify(red.attestation.content_sha256, red.interaction_id, red.attestation.signature):
            problems.append("invalid or missing attestation signature")
    if red.attestation.residual_findings:
        problems.append(f"{red.attestation.residual_findings} residual PII findings reported by edge")
    if redactor_for_scan is not None:
        n = sum(len(redactor_for_scan.residual_scan(t.text)) for t in red.turns if t.text)
        if n:
            problems.append(f"cloud re-scan found {n} residual PII spans")
    return problems

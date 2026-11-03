"""Output guard: runs on everything an agent/LLM produced before it can leave the orchestrator."""
from __future__ import annotations

import re

from convoiq.contracts import Analysis, GuardrailReport
from convoiq.models.classical import injection_heuristics
from convoiq.privacy.redactor import Redactor

COMMITMENT = re.compile(r"\b(i|we) (guarantee|promise|swear)\b|\bwill never happen again\b|\b100% (guaranteed|certain)\b", re.I)
LEGAL = re.compile(r"\b(legal advice|you should sue|you have a lawsuit|lawsuit)\b", re.I)
IMPERATIVE_TO_SYSTEM = re.compile(r"\b(ignore (?:the )?(?:previous|above)|disregard|as an ai|system prompt)\b", re.I)
MAX_SUMMARY_CHARS = 1500


def guard_output(analysis: Analysis, redactor: Redactor | None = None, check_instructions: bool = True) -> GuardrailReport:
    flags: list[str] = []
    text = analysis.summary or ""
    if len(text) > MAX_SUMMARY_CHARS:
        flags.append("summary_too_long")
    if COMMITMENT.search(text):
        flags.append("unsupported_commitment_in_summary")
    if LEGAL.search(text):
        flags.append("legal_advice_in_summary")
    # template summaries are built from structured fields only; instruction-like text matters when free-form (LLM) text is present
    if check_instructions and (IMPERATIVE_TO_SYSTEM.search(text) or injection_heuristics(text)):
        flags.append("instruction_like_text_in_summary")
    if redactor is not None and text:
        n = len(redactor.residual_scan(text))
        if n:
            flags.append(f"pii_in_output:{n}")
    for a in analysis.proposed_actions:
        note = str(a.params.get("note", "")) + str(a.rationale)
        if redactor is not None and note and redactor.residual_scan(note):
            flags.append(f"pii_in_action:{a.type.value}")
    blocked = any(f.startswith(("pii_in_", "instruction_like")) for f in flags)
    return GuardrailReport(blocked=blocked, input_flags=analysis.guardrail.input_flags, output_flags=flags,
                           reasons=[f"output guard: {f}" for f in flags])

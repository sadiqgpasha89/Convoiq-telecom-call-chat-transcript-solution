"""In-process pipeline (redact -> enrich -> analyze) used by evals, tests and ``make run-local``.

The deployed system runs the same three stages as separate Cloud Run services joined by Pub/Sub; sharing the
stage classes guarantees evals measure exactly what production executes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from convoiq.agents import AgentDeps, Orchestrator
from convoiq.agents.llm import LLMClient
from convoiq.agents.tools import TelecomContext
from convoiq.config import Settings, get_settings
from convoiq.contracts import Analysis, EnrichedInteraction, InteractionMeta, RawInteraction, RedactedInteraction
from convoiq.enrichment import Enricher
from convoiq.models.bundle import ModelBundle
from convoiq.nlp.text import corpus_turns_to_models
from convoiq.privacy import Redactor, Tokenizer
from convoiq.privacy.vault import TokenVault

_ID_BAD = re.compile(r"[^A-Za-z0-9_.:-]")


def corpus_to_raw(conv: dict, tenant: str = "default") -> RawInteraction:
    meta = InteractionMeta(**{k: v for k, v in (conv.get("meta") or {}).items() if k in InteractionMeta.model_fields})
    return RawInteraction(interaction_id=_ID_BAD.sub("_", conv["conv_id"])[:128], tenant_id=tenant, channel=conv.get("channel", "CHAT_APP"),
                          turns=corpus_turns_to_models(conv["turns"]), meta=meta)


@dataclass
class PipelineResult:
    redacted: RedactedInteraction
    enriched: EnrichedInteraction
    analysis: Analysis


class LocalPipeline:
    def __init__(self, settings: Settings | None = None, bundle: ModelBundle | None = None, tools: TelecomContext | None = None,
                 llm: LLMClient | None = None, vault: TokenVault | None = None, key: str = "local-dev-hmac-key-0123456789"):
        self.s = settings or get_settings()
        self.bundle = bundle or ModelBundle.load(self.s)
        self.redactor = Redactor(Tokenizer(self.s.pii_hmac_key or key), vault)
        self.enricher = Enricher(self.bundle, self.s)
        self.deps = AgentDeps.build(self.s, self.bundle, tools, llm)
        self.orchestrator = Orchestrator(self.deps)

    def redact(self, raw: RawInteraction) -> RedactedInteraction:
        return self.redactor.redact_interaction(raw)

    def enrich(self, red: RedactedInteraction) -> EnrichedInteraction:
        return self.enricher.enrich(red)

    async def process(self, raw: RawInteraction) -> PipelineResult:
        red = self.redact(raw)
        enr = self.enrich(red)
        return PipelineResult(red, enr, await self.orchestrator.analyze(enr))

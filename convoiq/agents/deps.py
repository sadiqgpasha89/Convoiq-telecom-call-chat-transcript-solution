"""Shared dependencies injected into every agent (explicit, testable, no globals)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from convoiq.agents.llm import GuardedLLM, LLMClient, NoLLM, make_llm
from convoiq.agents.tools import InMemoryTelecomTools, TelecomContext
from convoiq.config import Settings, get_settings
from convoiq.models.bundle import ModelBundle
from convoiq.privacy.redactor import Redactor
from convoiq.privacy.tokenizer import Tokenizer
from convoiq.rag.retriever import HybridRetriever


def load_remediation(data_dir: str | Path) -> dict[str, list[str]]:
    p = Path(data_dir) / "corpus" / "remediation_map.json"
    return json.loads(p.read_text()) if p.exists() else {}


@dataclass
class AgentDeps:
    settings: Settings
    bundle: ModelBundle
    retriever: HybridRetriever
    tools: TelecomContext
    remediation: dict[str, list[str]] = field(default_factory=dict)
    llm: LLMClient = field(default_factory=NoLLM)
    budget_ok: Callable[[], Awaitable[bool]] | None = None
    on_cost: Callable[[float], Awaitable[None]] | None = None
    scanner: Redactor | None = None            # residual-PII canary on outputs (no vault needed)

    @classmethod
    def build(cls, settings: Settings | None = None, bundle: ModelBundle | None = None, tools: TelecomContext | None = None,
              llm: LLMClient | None = None) -> "AgentDeps":
        s = settings or get_settings()
        bundle = bundle or ModelBundle.load(s)
        return cls(settings=s, bundle=bundle, retriever=HybridRetriever(embedder=bundle.embedder),
                   tools=tools or InMemoryTelecomTools(), remediation=load_remediation(s.data_dir),
                   llm=llm if llm is not None else make_llm(s),
                   scanner=Redactor(Tokenizer(s.pii_hmac_key or "scan-only-key-not-for-tokens")))

    def new_llm(self) -> GuardedLLM:
        s = self.settings
        return GuardedLLM(self.llm, max_calls=s.llm_max_calls_per_interaction, timeout_s=s.llm_timeout_s,
                          daily_budget_ok=self.budget_ok, on_cost=self.on_cost)

    @property
    def versions(self) -> dict[str, str]:
        return {**self.bundle.versions, "orchestrator": self.settings.version}

"""In-process wiring of ALL services over an in-memory bus (tests, demos, ``make run-local``).

Exercises the same service code, push handlers, idempotency, DLQ rules and HTTP contracts as production; only
Pub/Sub / Firestore / BigQuery / Cloud Run are replaced by in-memory/SQLite equivalents.
"""
from __future__ import annotations

import httpx

from convoiq.agents.tools import HttpTelecomTools
from convoiq.config import Settings, get_settings, set_settings
from convoiq.connectors import ConnectorRouter, SimulatedConnector
from convoiq.models.bundle import ModelBundle
from convoiq.platform.bus import InMemoryBus
from convoiq.platform.store import MemoryStore
from convoiq.platform.warehouse import SqliteWarehouse
from convoiq.privacy import Redactor, SqliteVault, Tokenizer
from convoiq.services import action_broker, analytics, enrichment, gateway, monitor, orchestrator, redaction, telecom_sim
from convoiq.services.common import Service


class LocalSystem:
    def __init__(self, settings: Settings | None = None, bundle: ModelBundle | None = None, llm=None, connector_router: ConnectorRouter | None = None):
        s = settings or Settings(env="local", auth_mode="none", connector_mode="simulated")
        set_settings(s)
        self.s = s
        self.bus = InMemoryBus(deliver=True, max_delivery_attempts=3)
        self.store = MemoryStore()
        self.wh = SqliteWarehouse(":memory:")
        self.bundle = bundle or ModelBundle.load(s)
        self.vault = SqliteVault()
        self.redactor = Redactor(Tokenizer(s.pii_hmac_key or "local-dev-hmac-key-0123456789"), self.vault)

        self.sim = telecom_sim.build(s, store=MemoryStore())
        sim_client = self._client(self.sim)
        tools = HttpTelecomTools(s.telecom_sim_url, s, client=sim_client)
        router = connector_router or ConnectorRouter([SimulatedConnector(s.telecom_sim_url, "", s, sim_client)])

        self.redaction = redaction.build(s, bus=self.bus, store=MemoryStore(), redactor=self.redactor)
        self.enrichment = enrichment.build(s, bus=self.bus, store=MemoryStore(), bundle=self.bundle)
        self.orchestrator = orchestrator.build(s, bus=self.bus, store=MemoryStore(), bundle=self.bundle, tools=tools, llm=llm)
        self.broker = action_broker.build(s, bus=self.bus, store=MemoryStore(), warehouse=self.wh, router=router)
        self.analytics = analytics.build(s, bus=self.bus, store=MemoryStore(), warehouse=self.wh)
        self.monitor = monitor.build(s, bus=self.bus, store=MemoryStore(), warehouse=self.wh)
        self.gateway = gateway.build(s, bus=self.bus, store=MemoryStore(), clients={
            "redaction": self._client(self.redaction), "enrichment": self._client(self.enrichment),
            "orchestrator": self._client(self.orchestrator), "analytics": self._client(self.analytics), "broker": self._client(self.broker)})

        self.services: list[Service] = [self.redaction, self.enrichment, self.orchestrator, self.broker, self.analytics, self.monitor]
        for svc in self.services:
            for topic, handler in svc.subs.items():
                self.bus.subscribe(topic, svc.name, handler)

    @staticmethod
    def _client(svc: Service) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=svc.app), base_url="http://svc", timeout=30.0)

    def client(self, which: str = "gateway") -> httpx.AsyncClient:
        return self._client(getattr(self, which))

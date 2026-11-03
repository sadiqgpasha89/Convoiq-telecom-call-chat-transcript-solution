"""Environment-driven configuration (12-factor, no external dependency).

Every knob is a ``CONVOIQ_*`` environment variable; secrets are expected to be injected by
Cloud Run / GKE from Secret Manager, never baked into images.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent


def _b(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in {"1", "true", "yes", "on"}


class Settings(BaseModel):
    env: str = "local"                       # local | dev | staging | prod
    service_name: str = "convoiq"
    log_level: str = "INFO"
    version: str = "2.0.0"

    # --- GCP ---------------------------------------------------------------------------
    gcp_project: str = ""
    gcp_region: str = "us-central1"
    bigquery_dataset: str = "convoiq_lakehouse"
    pubsub_topic_prefix: str = ""            # e.g. "dev-"

    # --- backends (memory/sqlite for local+tests, GCP for cloud) --------------------------
    bus_backend: str = "memory"              # memory | pubsub
    store_backend: str = "memory"            # memory | sqlite | firestore
    warehouse_backend: str = "sqlite"        # sqlite | bigquery
    cache_backend: str = "memory"            # memory | redis
    redis_url: str = "redis://localhost:6379/0"
    sqlite_path: str = ":memory:"

    # --- LLM ---------------------------------------------------------------------------
    llm_provider: str = "none"               # none | vertex | anthropic | scripted
    llm_model: str = "gemini-2.5-flash"
    llm_timeout_s: float = 20.0
    llm_max_calls_per_interaction: int = 6
    llm_daily_budget_usd: float = 200.0

    # --- security ------------------------------------------------------------------------
    auth_mode: str = "apikey"                # none | apikey | oidc
    api_keys: str = ""                       # "key1:ingest|analyst,key2:admin" (hashed in memory)
    oidc_audience: str = ""
    service_api_key: str = ""               # outbound credential for service-to-service calls when auth_mode=apikey
    allowed_service_accounts: str = ""       # OIDC principals: "email[:role|role],..." (bare email = service role)
    pii_hmac_key: str = ""                   # tokenisation key (Secret Manager)
    vault_key: str = ""                      # Fernet key for the token vault (edge only)
    attestation_key: str = ""                # edge->cloud HMAC for redaction attestation
    require_attestation: bool = False
    cloud_redaction_enabled: bool = True     # gateway accepts raw payloads and redacts in-zone

    # --- service URLs (service-to-service) -------------------------------------------------
    redaction_url: str = "http://redaction:8080"
    enrichment_url: str = "http://enrichment:8080"
    orchestrator_url: str = "http://orchestrator:8080"
    action_broker_url: str = "http://action-broker:8080"
    analytics_url: str = "http://analytics:8080"
    telecom_sim_url: str = "http://telecom-sim:8080"
    monitor_url: str = "http://monitor:8080"

    # --- connectors (action execution) -----------------------------------------------------------
    connector_mode: str = "simulated"        # simulated | dryrun | real
    salesforce_url: str = ""
    salesforce_token: str = ""              # injected from Secret Manager
    servicenow_url: str = ""
    servicenow_token: str = ""
    billing_url: str = ""
    billing_token: str = ""
    kill_switch: bool = False                # freeze all automated actions (also togglable at runtime via /admin/kill-switch)
    hitl_sample_rate: float = 0.02           # share of normal analyses routed to human QA review

    # --- assets -----------------------------------------------------------------------------
    models_dir: str = str(ROOT / "models")
    data_dir: str = str(ROOT / "data")
    kb_dir: str = str(ROOT / "data" / "kb")

    # --- business rules -----------------------------------------------------------------------
    credit_auto_limit_usd: float = 50.0       # above -> human approval
    credit_hard_limit_usd: float = 500.0      # above -> blocked outright
    retention_discount_max_pct: float = 25.0
    tier2_confidence_threshold: float = 0.80
    injection_block_threshold: float = 0.5   # >= -> quarantine (SAFE_MODE)
    fraud_quarantine_threshold: float = 0.6
    churn_high_threshold: float = 0.6
    bss_timeout_s: float = 0.8
    circuit_fail_threshold: int = 5
    circuit_reset_s: float = 30.0
    psi_alert: float = 0.10
    psi_retrain: float = 0.25

    @classmethod
    def from_env(cls, prefix: str = "CONVOIQ_") -> "Settings":
        kw: dict = {}
        for name, field in cls.model_fields.items():
            raw = os.getenv(prefix + name.upper())
            if raw is None:
                continue
            ann = field.annotation
            if ann is bool:
                kw[name] = _b(prefix + name.upper(), bool(field.default))
            elif ann is int:
                kw[name] = int(raw)
            elif ann is float:
                kw[name] = float(raw)
            else:
                kw[name] = raw
        return cls(**kw)

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    def validate_for_runtime(self) -> list[str]:
        """Return a list of fatal misconfigurations (prod must not run with dev defaults)."""
        problems = []
        if self.is_prod:
            if self.auth_mode == "none":
                problems.append("auth_mode=none is not allowed in prod")
            if not self.pii_hmac_key:
                problems.append("pii_hmac_key must be set in prod")
            if self.bus_backend == "memory":
                problems.append("bus_backend=memory is not allowed in prod")
            if self.store_backend in {"memory", "sqlite"}:
                problems.append("store_backend must be firestore in prod")
            if self.auth_mode == "oidc" and not self.oidc_audience:
                problems.append("oidc_audience required for oidc auth")
        return problems


_override: Settings | None = None


@lru_cache(maxsize=1)
def _cached() -> Settings:
    return Settings.from_env()


def get_settings() -> Settings:
    return _override or _cached()


def set_settings(s: Settings | None) -> None:
    """Test hook."""
    global _override
    _override = s
    _cached.cache_clear()

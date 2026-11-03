"""Shared fixtures.  Model-dependent tests need trained models (``make train`` or ``make train-eval``):
set CONVOIQ_TEST_MODELS to point elsewhere.  Tests are plain sync functions using ``asyncio.run`` (no pytest-asyncio needed)."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from convoiq.config import Settings, set_settings  # noqa: E402

MODELS = Path(os.environ.get("CONVOIQ_TEST_MODELS", ROOT / "models"))
HAVE_MODELS = (MODELS / "MANIFEST.json").exists()
needs_models = pytest.mark.skipif(not HAVE_MODELS, reason=f"no trained models in {MODELS} (run `make train`)")


def arun(coro):
    return asyncio.run(coro)


def make_conv(turns: list[tuple[str, str]], channel: str = "CHAT_APP", conv_id: str = "t-1", meta: dict | None = None) -> dict:
    """Hand-written conversation in the corpus-dict format: turns = [(role, text), ...]."""
    return {"conv_id": conv_id, "channel": channel, "meta": meta or {},
            "turns": [{"i": i, "role": r, "text": x, "tool_calls": []} for i, (r, x) in enumerate(turns)]}


@pytest.fixture(scope="session")
def settings() -> Settings:
    s = Settings(env="local", auth_mode="none", models_dir=str(MODELS))
    set_settings(s)
    return s


@pytest.fixture(scope="session")
def bundle(settings):
    if not HAVE_MODELS:
        pytest.skip("no trained models")
    from convoiq.models.bundle import ModelBundle
    return ModelBundle.load(settings, MODELS)


@pytest.fixture(scope="session")
def pipeline(settings, bundle):
    from convoiq.pipeline import LocalPipeline
    return LocalPipeline(settings, bundle)


@pytest.fixture()
def enrich(pipeline):
    from convoiq.pipeline import corpus_to_raw

    def _e(conv: dict):
        return pipeline.enrich(pipeline.redact(corpus_to_raw(conv)))
    return _e


@pytest.fixture()
def redactor():
    from convoiq.privacy import Redactor, SqliteVault, Tokenizer
    return Redactor(Tokenizer("unit-test-hmac-key-0123456789abcdef"), SqliteVault())

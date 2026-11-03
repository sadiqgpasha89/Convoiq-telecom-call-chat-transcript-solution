"""Data generators, split hygiene, schemas/warehouse portability, stores and bundle degradation."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pytest

from convoiq.data.noise import asr_noise, wer
from convoiq.data.synth import generate
from convoiq.models.bundle import ModelBundle
from convoiq.platform import schemas
from convoiq.platform.store import MemoryStore
from convoiq.platform.warehouse import SqliteWarehouse
from convoiq.training.datasets import tau2_split

from .conftest import arun


def test_synth_is_deterministic():
    a, b = generate(20, seed=5), generate(20, seed=5)
    assert [c["conv_id"] for c in a] == [c["conv_id"] for c in b]
    assert [c["turns"] for c in a] == [c["turns"] for c in b]
    assert [c["conv_id"] for c in generate(20, seed=6)] != [c["conv_id"] for c in a]


def test_noise_changes_text_but_not_the_underlying_conversation():
    """Regression: noise used to consume the content RNG, so 'noisy' and 'clean' were different conversations."""
    clean, noisy = generate(60, seed=9), generate(60, seed=9, noise_rate=0.3)
    assert [c["conv_id"] for c in clean] == [c["conv_id"] for c in noisy]
    assert [c["labels"]["family"] for c in clean] == [c["labels"]["family"] for c in noisy]
    changed = sum(a["text"] != b["text"] for ca, cb in zip(clean, noisy) for a, b in zip(ca["turns"], cb["turns"]) if a["role"] == "customer")
    assert changed > 50
    for ca, cb in zip(clean, noisy):                         # agent turns are untouched (ASR noise is on the customer's speech)
        assert [t["text"] for t in ca["turns"] if t["role"] == "agent"] == [t["text"] for t in cb["turns"] if t["role"] == "agent"]


def test_asr_noise_is_deterministic_and_wer_grows_with_rate():
    t = "I was charged twice for my plan this month and I want a refund please"
    assert asr_noise(t, 0.3, seed=1) == asr_noise(t, 0.3, seed=1)
    base = asr_noise(t, 0.0, seed=1)
    w = [sum(wer(base, asr_noise(t, r, seed=s)) for s in range(200)) / 200 for r in (0.05, 0.15, 0.35)]
    assert w[0] < w[1] < w[2]
    assert wer("a b c", "a b c") == 0 and wer("a b c", "") == 1.0


def test_tau2_train_test_split_is_disjoint_and_official_sized():
    sp = tau2_split()
    assert sp["train"].isdisjoint(sp["test"]) and (len(sp["train"]), len(sp["test"])) == (74, 40)


def test_bundle_load_degrades_when_models_missing(tmp_path, settings):
    b = ModelBundle.load(settings, tmp_path)
    assert b.issue is None and set(b.missing) >= {"issue", "churn"}


def test_every_table_has_matching_sqlite_and_bigquery_definitions():
    for t in schemas.TABLES:
        cols = schemas.column_names(t)
        assert cols and len(cols) == len(set(cols)), t
        assert [f["name"] for f in schemas.bq_schema(t)] == cols
        assert t in schemas.sqlite_ddl(t)


def test_warehouse_roundtrip_and_dialect_helpers():
    async def main():
        wh = SqliteWarehouse(":memory:")
        row = {c: None for c in schemas.column_names("interactions_fact")}
        row.update(interaction_id="i-1", tenant_id="default", started_at=datetime(2026, 9, 1, 10, tzinfo=timezone.utc), issue_type="NO_SERVICE")
        assert await wh.insert("interactions_fact", [row]) == 1
        hb, tbl = wh.hour_bucket("started_at"), wh.t("interactions_fact")
        got = await wh.query(f"SELECT interaction_id, {hb} AS h FROM {tbl} WHERE issue_type = {wh.p('it')}",
                             {"it": "NO_SERVICE"})
        assert got == [{"interaction_id": "i-1", "h": "2026-09-01T10"}]
    arun(main())


def test_store_put_if_absent_is_atomic_under_concurrency():
    async def main():
        st = MemoryStore()
        wins = await asyncio.gather(*[st.put_if_absent("c", "k", {"n": i}) for i in range(50)])
        assert sum(wins) == 1
    arun(main())


def test_store_ttl_expiry():
    async def main():
        st = MemoryStore()
        await st.put("c", "k", {"v": 1}, ttl=0.05)
        assert await st.get("c", "k") == {"v": 1}
        await asyncio.sleep(0.08)
        assert await st.get("c", "k") is None
    arun(main())

"""Document/KV store abstraction (state, idempotency, session cache, HITL queue, registries).

Backends: memory (tests), sqlite (single-node dev), Firestore (GCP, native TTL policies).  Redis-style
cache with TTL is the same interface (``ttl`` argument) - Memorystore is optional, Firestore TTL is the
default so the baseline deployment needs no VPC connector.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Protocol

from convoiq.config import Settings, get_settings


class Store(Protocol):
    async def get(self, coll: str, key: str) -> dict | None: ...
    async def put(self, coll: str, key: str, doc: dict, ttl: float | None = None) -> None: ...
    async def put_if_absent(self, coll: str, key: str, doc: dict, ttl: float | None = None) -> bool: ...
    async def delete(self, coll: str, key: str) -> None: ...
    async def list(self, coll: str, prefix: str = "", limit: int = 100) -> list[dict]: ...


class MemoryStore:
    def __init__(self):
        self._d: dict[tuple[str, str], tuple[dict, float | None]] = {}
        self._lock = threading.Lock()

    def _alive(self, e) -> bool:
        return e[1] is None or e[1] > time.time()

    async def get(self, coll, key):
        e = self._d.get((coll, key))
        return json.loads(json.dumps(e[0])) if e and self._alive(e) else None

    async def put(self, coll, key, doc, ttl=None):
        with self._lock:
            self._d[(coll, key)] = (json.loads(json.dumps(doc, default=str)), time.time() + ttl if ttl else None)

    async def put_if_absent(self, coll, key, doc, ttl=None):
        with self._lock:
            e = self._d.get((coll, key))
            if e and self._alive(e):
                return False
            self._d[(coll, key)] = (json.loads(json.dumps(doc, default=str)), time.time() + ttl if ttl else None)
            return True

    async def delete(self, coll, key):
        self._d.pop((coll, key), None)

    async def list(self, coll, prefix="", limit=100):
        out = [v[0] for (c, k), v in sorted(self._d.items()) if c == coll and k.startswith(prefix) and self._alive(v)]
        return out[:limit]


class SqliteStore:
    def __init__(self, path: str = ":memory:"):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("CREATE TABLE IF NOT EXISTS kv(coll TEXT, k TEXT, doc TEXT, exp REAL, PRIMARY KEY(coll,k))")

    async def get(self, coll, key):
        with self._lock:
            r = self._db.execute("SELECT doc,exp FROM kv WHERE coll=? AND k=?", (coll, key)).fetchone()
        if not r or (r[1] is not None and r[1] < time.time()):
            return None
        return json.loads(r[0])

    async def put(self, coll, key, doc, ttl=None):
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO kv VALUES(?,?,?,?)", (coll, key, json.dumps(doc, default=str), time.time() + ttl if ttl else None))
            self._db.commit()

    async def put_if_absent(self, coll, key, doc, ttl=None):
        with self._lock:
            r = self._db.execute("SELECT exp FROM kv WHERE coll=? AND k=?", (coll, key)).fetchone()
            if r and (r[0] is None or r[0] > time.time()):
                return False
            self._db.execute("INSERT OR REPLACE INTO kv VALUES(?,?,?,?)", (coll, key, json.dumps(doc, default=str), time.time() + ttl if ttl else None))
            self._db.commit()
            return True

    async def delete(self, coll, key):
        with self._lock:
            self._db.execute("DELETE FROM kv WHERE coll=? AND k=?", (coll, key))
            self._db.commit()

    async def list(self, coll, prefix="", limit=100):
        with self._lock:
            rows = self._db.execute("SELECT doc FROM kv WHERE coll=? AND k LIKE ? AND (exp IS NULL OR exp>?) ORDER BY k LIMIT ?",
                                    (coll, prefix + "%", time.time(), limit)).fetchall()
        return [json.loads(r[0]) for r in rows]


class FirestoreStore:
    """Firestore (native mode) backend. Create a TTL policy on the ``_expires_at`` field per collection."""

    def __init__(self, project: str, prefix: str = "convoiq"):
        from google.cloud import firestore  # lazy
        self._fs = firestore
        self._db = firestore.AsyncClient(project=project)
        self.prefix = prefix

    def _ref(self, coll: str, key: str):
        return self._db.collection(f"{self.prefix}_{coll}").document(key.replace("/", "%2F"))

    async def get(self, coll, key):
        snap = await self._ref(coll, key).get()
        if not snap.exists:
            return None
        d = snap.to_dict() or {}
        exp = d.pop("_expires_at", None)
        if exp and exp.timestamp() < time.time():
            return None
        return json.loads(d["_doc"]) if "_doc" in d else d

    def _payload(self, doc: dict, ttl: float | None) -> dict:
        p: dict[str, Any] = {"_doc": json.dumps(doc, default=str)}
        if ttl:
            import datetime as dt
            p["_expires_at"] = dt.datetime.fromtimestamp(time.time() + ttl, dt.timezone.utc)
        return p

    async def put(self, coll, key, doc, ttl=None):
        await self._ref(coll, key).set(self._payload(doc, ttl))

    async def put_if_absent(self, coll, key, doc, ttl=None):
        from google.api_core.exceptions import AlreadyExists
        try:
            await self._ref(coll, key).create(self._payload(doc, ttl))
            return True
        except AlreadyExists:
            existing = await self.get(coll, key)          # expired tombstone?
            if existing is None:
                await self.put(coll, key, doc, ttl)
                return True
            return False

    async def delete(self, coll, key):
        await self._ref(coll, key).delete()

    async def list(self, coll, prefix="", limit=100):
        out = []
        async for snap in self._db.collection(f"{self.prefix}_{coll}").limit(limit).stream():
            if snap.id.startswith(prefix):
                d = snap.to_dict() or {}
                out.append(json.loads(d["_doc"]) if "_doc" in d else d)
        return out


def make_store(settings: Settings | None = None) -> Store:
    s = settings or get_settings()
    if s.store_backend == "firestore":
        return FirestoreStore(s.gcp_project)
    if s.store_backend == "sqlite":
        return SqliteStore(s.sqlite_path)
    return MemoryStore()


class Idempotency:
    """Exactly-once *effects* on top of at-least-once delivery."""

    def __init__(self, store: Store, scope: str, ttl: float = 7 * 24 * 3600):
        self.store, self.scope, self.ttl = store, scope, ttl

    async def first_time(self, key: str) -> bool:
        return await self.store.put_if_absent(f"idem_{self.scope}", key, {"t": time.time()}, self.ttl)

    async def release(self, key: str) -> None:
        await self.store.delete(f"idem_{self.scope}", key)

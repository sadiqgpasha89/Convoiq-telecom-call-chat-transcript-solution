"""Token vault: reversible lookup for authorised, audited re-identification (edge/sovereign zone only).

The cloud analytics plane never has access to the vault.  Values are encrypted at rest with Fernet
(AES-128-CBC + HMAC); in production the key is a CMEK-wrapped secret.  ``erase_subject`` implements
GDPR Art.17 "right to erasure": deleting the vault entries makes every downstream token permanently
un-resolvable without touching the analytical history.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from typing import Protocol

from cryptography.fernet import Fernet


class TokenVault(Protocol):
    def put(self, token: str, typ: str, value: str, subject: str | None = None) -> None: ...
    def get(self, token: str, actor: str, purpose: str) -> str | None: ...
    def erase_subject(self, subject: str) -> int: ...


class SqliteVault:
    def __init__(self, key: str | bytes | None = None, path: str = ":memory:"):
        self._f = Fernet(key or Fernet.generate_key())
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("CREATE TABLE IF NOT EXISTS vault(token TEXT PRIMARY KEY, typ TEXT, value BLOB, subject TEXT, ts REAL)")
            self._db.execute("CREATE TABLE IF NOT EXISTS vault_audit(ts REAL, actor TEXT, purpose TEXT, token TEXT, found INT)")
            self._db.execute("CREATE INDEX IF NOT EXISTS ix_subject ON vault(subject)")

    def put(self, token: str, typ: str, value: str, subject: str | None = None) -> None:
        enc = self._f.encrypt(value.encode())
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO vault VALUES(?,?,?,?,?)", (token, typ, enc, subject, time.time()))
            self._db.commit()

    def get(self, token: str, actor: str, purpose: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM vault WHERE token=?", (token,)).fetchone()
            self._db.execute("INSERT INTO vault_audit VALUES(?,?,?,?,?)", (time.time(), actor, purpose, token, int(bool(row))))
            self._db.commit()
        return self._f.decrypt(row[0]).decode() if row else None

    def erase_subject(self, subject: str) -> int:
        with self._lock:
            cur = self._db.execute("DELETE FROM vault WHERE subject=?", (subject,))
            self._db.commit()
            return cur.rowcount

    def audit_log(self) -> list[tuple]:
        with self._lock:
            return list(self._db.execute("SELECT * FROM vault_audit ORDER BY ts"))


class FirestoreVault:
    """Durable vault for Cloud Run (the sqlite file would vanish with the instance).  Same encryption + audit semantics.

    Note: written against the public google-cloud-firestore API; not exercised against a live project in this repo's CI.
    """

    def __init__(self, project: str, key: str | bytes, prefix: str = "convoiq"):
        from google.cloud import firestore  # lazy
        self._fs = firestore
        self._db = firestore.Client(project=project)
        self._f = Fernet(key)
        self._v, self._a = f"{prefix}_vault", f"{prefix}_vault_audit"

    def put(self, token: str, typ: str, value: str, subject: str | None = None) -> None:
        ref = self._db.collection(self._v).document(token)
        if not ref.get().exists:
            ref.set({"typ": typ, "value": self._f.encrypt(value.encode()), "subject": subject, "ts": time.time()})

    def get(self, token: str, actor: str, purpose: str) -> str | None:
        snap = self._db.collection(self._v).document(token).get()
        self._db.collection(self._a).add({"ts": time.time(), "actor": actor, "purpose": purpose, "token": token, "found": snap.exists})
        return self._f.decrypt(snap.to_dict()["value"]).decode() if snap.exists else None

    def erase_subject(self, subject: str) -> int:
        n = 0
        for d in self._db.collection(self._v).where("subject", "==", subject).stream():
            d.reference.delete()
            n += 1
        return n


def make_vault(settings) -> TokenVault:
    """sqlite for local/tests, Firestore on GCP.  The Fernet key must be supplied in non-local environments."""
    if settings.store_backend == "firestore":
        return FirestoreVault(settings.gcp_project, settings.vault_key)
    return SqliteVault(settings.vault_key or None, settings.sqlite_path.replace(".db", "-vault.db") if settings.sqlite_path != ":memory:" else ":memory:")

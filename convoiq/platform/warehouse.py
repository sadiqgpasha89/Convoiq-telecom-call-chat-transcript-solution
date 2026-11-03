"""Analytical warehouse abstraction (SQLite for local/tests, BigQuery on GCP)."""
from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import datetime
from typing import Any, Protocol

from convoiq.config import Settings, get_settings
from convoiq.platform import schemas


class Warehouse(Protocol):
    dialect: str
    def t(self, table: str) -> str: ...
    def p(self, name: str) -> str: ...
    def ts(self, name: str) -> str: ...
    def hour_bucket(self, col: str) -> str: ...
    def day_bucket(self, col: str) -> str: ...
    async def insert(self, table: str, rows: list[dict]) -> int: ...
    async def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]: ...


def _coerce(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (list, dict)):
        import json
        return json.dumps(v, default=str)
    return v


class SqliteWarehouse:
    dialect = "sqlite"

    def __init__(self, path: str = ":memory:"):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        for t in schemas.TABLES:
            self._db.execute(schemas.sqlite_ddl(t))
        self._db.commit()

    async def insert(self, table: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        cols = schemas.column_names(table)
        with self._lock:
            self._db.executemany(
                f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [[_coerce(r.get(c)) for c in cols] for r in rows])
            self._db.commit()
        return len(rows)

    async def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        with self._lock:
            cur = self._db.execute(sql, params or {})
            return [dict(r) for r in cur.fetchall()]

    # helpers for dialect-specific SQL
    @staticmethod
    def t(table: str) -> str:
        return table

    @staticmethod
    def p(name: str) -> str:
        return f":{name}"

    @staticmethod
    def ts(name: str) -> str:
        return f":{name}"

    @staticmethod
    def hour_bucket(col: str) -> str:
        return f"substr({col},1,13)"

    @staticmethod
    def day_bucket(col: str) -> str:
        return f"substr({col},1,10)"


class BigQueryWarehouse:
    dialect = "bigquery"

    def __init__(self, project: str, dataset: str):
        from google.cloud import bigquery  # lazy
        self._bq = bigquery
        self.client = bigquery.Client(project=project)
        self.project, self.dataset = project, dataset

    def _t(self, table: str) -> str:
        return f"{self.project}.{self.dataset}.{table}"

    def t(self, table: str) -> str:
        """Qualified, quoted table reference for use inside SQL strings."""
        return f"`{self._t(table)}`"

    async def insert(self, table: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        cols = schemas.column_names(table)
        payload = [{c: (r[c].isoformat() if isinstance(r.get(c), datetime) else r.get(c)) for c in cols if r.get(c) is not None} for r in rows]
        ids = [f"{r.get('interaction_id') or r.get('action_id') or r.get('incident_id') or r.get('report_id') or r.get('review_id')}-{i}" for i, r in enumerate(rows)]
        loop = asyncio.get_running_loop()
        errors = await loop.run_in_executor(None, lambda: self.client.insert_rows_json(self._t(table), payload, row_ids=ids))
        if errors:
            raise RuntimeError(f"BigQuery insert errors: {errors[:3]}")
        return len(rows)

    async def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        cfg = self._bq.QueryJobConfig(query_parameters=[
            self._bq.ScalarQueryParameter(k, "FLOAT64" if isinstance(v, float) else "INT64" if isinstance(v, int) else "STRING", v)
            for k, v in (params or {}).items()])
        loop = asyncio.get_running_loop()
        job = await loop.run_in_executor(None, lambda: self.client.query(sql, job_config=cfg).result())
        return [dict(r.items()) for r in job]

    @staticmethod
    def p(name: str) -> str:
        return f"@{name}"

    @staticmethod
    def ts(name: str) -> str:
        return f"TIMESTAMP(@{name})"

    @staticmethod
    def hour_bucket(col: str) -> str:
        return f"FORMAT_TIMESTAMP('%Y-%m-%dT%H', TIMESTAMP({col}))"

    @staticmethod
    def day_bucket(col: str) -> str:
        return f"FORMAT_TIMESTAMP('%Y-%m-%d', TIMESTAMP({col}))"


def make_warehouse(settings: Settings | None = None) -> Warehouse:
    s = settings or get_settings()
    if s.warehouse_backend == "bigquery":
        return BigQueryWarehouse(s.gcp_project, s.bigquery_dataset)
    return SqliteWarehouse(s.sqlite_path if s.sqlite_path != ":memory:" else ":memory:")

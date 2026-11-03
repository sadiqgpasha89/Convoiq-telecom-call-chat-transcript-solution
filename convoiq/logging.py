"""Structured JSON logging compatible with Cloud Logging (severity, trace correlation, no PII)."""
from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
from typing import Any

_ctx: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("convoiq_log_ctx", default={})
_SEVERITY = {"DEBUG": "DEBUG", "INFO": "INFO", "WARNING": "WARNING", "ERROR": "ERROR", "CRITICAL": "CRITICAL"}


def bind(**kv: Any) -> None:
    cur = dict(_ctx.get())
    cur.update({k: v for k, v in kv.items() if v is not None})
    _ctx.set(cur)


def clear() -> None:
    _ctx.set({})


def current() -> dict[str, Any]:
    return dict(_ctx.get())


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str, project: str = ""):
        super().__init__()
        self.service, self.project = service, project

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "severity": _SEVERITY.get(record.levelname, "DEFAULT"),
            "message": record.getMessage(),
            "service": self.service,
            "logger": record.name,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
        }
        ctx = _ctx.get()
        entry.update(ctx)
        trace = ctx.get("trace_id")
        if trace and self.project:
            entry["logging.googleapis.com/trace"] = f"projects/{self.project}/traces/{trace}"
        extra = getattr(record, "fields", None)
        if extra:
            entry.update(extra)
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure(service: str, level: str = "INFO", project: str = "") -> None:
    root = logging.getLogger()
    root.handlers.clear()
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter(service, project))
    root.addHandler(h)
    root.setLevel(level)
    for noisy in ("uvicorn.access", "httpx", "httpcore", "httpx2", "httpcore2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class Log:
    """Thin wrapper: ``log.info("event", key=value)`` -> JSON with fields. Never log raw PII."""

    def __init__(self, name: str):
        self._l = logging.getLogger(name)

    def _emit(self, level: int, msg: str, **fields: Any) -> None:
        self._l.log(level, msg, extra={"fields": fields} if fields else None)

    def debug(self, msg: str, **f: Any) -> None: self._emit(logging.DEBUG, msg, **f)
    def info(self, msg: str, **f: Any) -> None: self._emit(logging.INFO, msg, **f)
    def warning(self, msg: str, **f: Any) -> None: self._emit(logging.WARNING, msg, **f)
    def error(self, msg: str, **f: Any) -> None: self._emit(logging.ERROR, msg, **f)
    def exception(self, msg: str, **f: Any) -> None:
        self._l.exception(msg, extra={"fields": f} if f else None)


def get_logger(name: str) -> Log:
    return Log(name)


def current_trace_id() -> str | None:
    return current().get("trace_id") or current().get("request_id")

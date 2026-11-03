"""FastAPI service factory shared by all ConvoIQ microservices.

Gives every service, uniformly: JSON logging with trace correlation, request-id propagation, latency
metrics, health/readiness probes, `/metrics`, secure headers, body-size limit, per-principal rate
limiting, RFC-7807 error bodies that never leak internals, and the Pub/Sub push-consumer contract
(idempotent, DLQ on poison messages, 5xx on transient failures so Pub/Sub redelivers).
"""
from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from typing import Awaitable, Callable

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from convoiq import logging as clog
from convoiq.config import Settings, get_settings
from convoiq.contracts import EventEnvelope
from convoiq.logging import get_logger
from convoiq.platform.auth import Authenticator, Principal, require
from convoiq.platform.bus import EventBus, PermanentError, decode_push, dead_letter, make_bus
from convoiq.platform.metrics import REGISTRY
from convoiq.platform.resilience import TokenBucket
from convoiq.platform.store import Idempotency, Store, make_store

log = get_logger("convoiq.service")
_req_dur = REGISTRY.histogram("convoiq_http_request_duration_seconds", "HTTP latency", ["service", "method", "route", "status"])
_req_total = REGISTRY.counter("convoiq_http_requests_total", "HTTP requests", ["service", "method", "route", "status"])
_evt_total = REGISTRY.counter("convoiq_events_consumed_total", "events consumed", ["service", "type", "outcome"])
_evt_dur = REGISTRY.histogram("convoiq_event_handle_seconds", "event handling latency", ["service", "type"])

MAX_BODY = 2 * 1024 * 1024
Handler = Callable[[EventEnvelope], Awaitable[None]]


def parse_trace(header: str | None) -> str | None:
    """X-Cloud-Trace-Context: TRACE_ID/SPAN_ID;o=1 -> TRACE_ID"""
    if not header:
        return None
    return header.split("/")[0].strip() or None


def create_app(name: str, description: str = "", settings: Settings | None = None, bus: EventBus | None = None,
               store: Store | None = None, rate_per_s: float = 50.0, burst: int = 200,
               on_startup: Callable[[FastAPI], Awaitable[None]] | None = None,
               on_shutdown: Callable[[FastAPI], Awaitable[None]] | None = None) -> FastAPI:
    s = settings or get_settings()
    clog.configure(name, s.log_level, s.gcp_project)
    problems = s.validate_for_runtime()
    if problems:
        raise RuntimeError("invalid configuration: " + "; ".join(problems))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if on_startup:
            await on_startup(app)
        log.info("service started", env=s.env, version=s.version)
        yield
        if on_shutdown:
            await on_shutdown(app)

    app = FastAPI(title=f"ConvoIQ {name}", description=description, version=s.version, lifespan=lifespan,
                  docs_url=None if s.is_prod else "/docs", redoc_url=None, openapi_url=None if s.is_prod else "/openapi.json")
    app.state.settings = s
    app.state.service = name
    app.state.bus = bus or make_bus(s)
    app.state.store = store or make_store(s)
    app.state.auth = Authenticator(s)
    app.state.ready_checks = {}
    app.state.limiter = TokenBucket(rate_per_s, burst)

    @app.middleware("http")
    async def _mw(request: Request, call_next):
        t0 = time.perf_counter()
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        trace = parse_trace(request.headers.get("x-cloud-trace-context")) or rid
        clog.clear()
        clog.bind(request_id=rid, trace_id=trace)
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            return JSONResponse({"type": "about:blank", "title": "Payload too large", "status": 413}, status_code=413)
        key = request.headers.get("authorization", request.client.host if request.client else "anon")[-24:]
        if request.url.path not in ("/healthz", "/readyz", "/metrics") and not app.state.limiter.allow(key):
            return JSONResponse({"type": "about:blank", "title": "Too many requests", "status": 429}, status_code=429,
                                headers={"Retry-After": "1"})
        try:
            response = await call_next(request)
        except Exception:
            log.exception("unhandled error", path=request.url.path)
            response = JSONResponse({"type": "about:blank", "title": "Internal error", "status": 500, "request_id": rid}, status_code=500)
        dur = time.perf_counter() - t0
        route = getattr(request.scope.get("route"), "path", request.url.path)
        labels = dict(service=name, method=request.method, route=route, status=str(response.status_code))
        _req_dur.observe(dur, **labels)
        _req_total.inc(**labels)
        response.headers["x-request-id"] = rid
        response.headers["x-content-type-options"] = "nosniff"
        response.headers["cache-control"] = "no-store"
        response.headers["strict-transport-security"] = "max-age=63072000; includeSubDomains"
        return response

    @app.exception_handler(HTTPException)
    async def _http_exc(request: Request, exc: HTTPException):
        return JSONResponse({"type": "about:blank", "title": str(exc.detail), "status": exc.status_code}, status_code=exc.status_code,
                            headers=getattr(exc, "headers", None))

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok", "service": name, "version": s.version}

    @app.get("/readyz", include_in_schema=False)
    async def readyz():
        results = {}
        for k, fn in app.state.ready_checks.items():
            try:
                results[k] = bool(await fn())
            except Exception:
                results[k] = False
        ok = all(results.values())
        return JSONResponse({"ready": ok, "checks": results}, status_code=200 if ok else 503)

    async def _metrics_guard(request: Request) -> None:
        # internal-only: outside local dev the scraper must present a service/admin identity
        if s.env != "local" and s.auth_mode != "none":
            if not app.state.auth.authenticate(request).has("service"):
                raise HTTPException(403, "metrics are internal")

    @app.get("/metrics", include_in_schema=False, dependencies=[Depends(_metrics_guard)])
    async def metrics():
        return PlainTextResponse(REGISTRY.render(), media_type="text/plain; version=0.0.4")

    return app


def wrap_consumer(service: str, handler: Handler, bus: EventBus, store: Store, scope: str | None = None) -> Handler:
    """Idempotent, observable, DLQ-aware consumer wrapper - identical for Pub/Sub push and the local bus."""
    idem = Idempotency(store, scope or service)

    async def _wrapped(ev: EventEnvelope) -> None:
        key = f"{service}:{ev.event_id}"
        if not await idem.first_time(key):
            _evt_total.inc(service=service, type=ev.type, outcome="duplicate")
            return
        clog.bind(event_id=ev.event_id, trace_id=ev.trace_id)
        t0 = time.perf_counter()
        try:
            await handler(ev)
            _evt_total.inc(service=service, type=ev.type, outcome="ok")
        except PermanentError as e:
            await dead_letter(bus, ev, str(e), service)
            _evt_total.inc(service=service, type=ev.type, outcome="dead_letter")
            log.error("event dead-lettered", reason=str(e), event_type=ev.type)
        except Exception:
            await idem.release(key)               # allow redelivery to run again
            _evt_total.inc(service=service, type=ev.type, outcome="error")
            raise
        finally:
            _evt_dur.observe(time.perf_counter() - t0, service=service, type=ev.type)

    return _wrapped


def add_push_consumer(app: FastAPI, path: str, handler: Handler) -> Handler:
    """Register a Pub/Sub push endpoint. Returns the wrapped handler (reused by the local bus)."""
    wrapped = wrap_consumer(app.state.service, handler, app.state.bus, app.state.store)

    @app.post(path, status_code=204, include_in_schema=False)
    async def _push(request: Request, _: Principal = Depends(require("service"))):
        try:
            ev = decode_push(await request.json())
        except PermanentError as e:
            log.error("malformed push", error=str(e))
            return Response(status_code=204)      # ack: nothing a retry can fix
        await wrapped(ev)                          # exceptions -> 500 -> Pub/Sub redelivers / DLQ
        return Response(status_code=204)

    return wrapped

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx
from fastapi import FastAPI

from convoiq.config import Settings, get_settings
from convoiq.contracts import EventEnvelope
from convoiq.logging import current_trace_id

Handler = Callable[[EventEnvelope], Awaitable[None]]


@dataclass
class Service:
    """A built service: the ASGI app plus the topic->handler map (used by the in-process runner instead of Pub/Sub push)."""
    name: str
    app: FastAPI
    subs: dict[str, Handler] = field(default_factory=dict)
    state: Any = None


def make_event(type_: str, payload: dict[str, Any], source: str, ordering_key: str | None = None, trace_id: str | None = None) -> EventEnvelope:
    return EventEnvelope(type=type_, source=source, payload=payload, ordering_key=ordering_key, trace_id=trace_id or current_trace_id())


class TokenProvider:
    """Outbound service identity: Google ID token (Cloud Run IAM) or static API key; cached until shortly before expiry."""

    def __init__(self, settings: Settings, audience: str):
        self.s, self.aud = settings, audience
        self._tok: str | None = None
        self._exp = 0.0

    async def __call__(self) -> str | None:
        if self.s.auth_mode == "apikey":
            return self.s.service_api_key or None
        if self.s.auth_mode != "oidc":
            return None
        if self._tok and time.time() < self._exp:
            return self._tok

        def fetch() -> str:
            import google.auth.transport.requests
            from google.oauth2 import id_token
            return id_token.fetch_id_token(google.auth.transport.requests.Request(), self.aud)

        self._tok = await asyncio.get_running_loop().run_in_executor(None, fetch)
        self._exp = time.time() + 45 * 60
        return self._tok


class ServiceClient:
    def __init__(self, base_url: str, settings: Settings | None = None, client: httpx.AsyncClient | None = None, timeout: float = 15.0):
        self.s = settings or get_settings()
        self.base = base_url.rstrip("/")
        self.client = client or httpx.AsyncClient(timeout=timeout)
        self.tokens = TokenProvider(self.s, self.base)

    async def request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        headers = dict(kw.pop("headers", {}) or {})
        tok = await self.tokens()
        if tok:
            headers["authorization"] = f"Bearer {tok}"
        trace = current_trace_id()
        if trace:
            headers["x-request-id"] = trace
        return await self.client.request(method, self.base + path, headers=headers, **kw)

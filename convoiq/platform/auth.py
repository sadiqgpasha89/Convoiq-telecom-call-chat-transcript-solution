"""Authentication & RBAC.

* ``apikey``  - static keys for partners / local dev: ``CONVOIQ_API_KEYS="k1:ingest|analyst,k2:admin"``
                (only SHA-256 digests are kept in memory; compared in constant time).
* ``oidc``    - Google-signed ID tokens (Cloud Run IAM / IAP / Pub/Sub push / service accounts). The
                token's audience and email are verified; roles come from the ``allowed_service_accounts`` map (email[:role|role]).
* ``none``    - local only (refused in prod by ``Settings.validate_for_runtime``).

Roles: ``ingest`` (producers), ``analyst`` (read insights), ``approver`` (approve high-risk actions),
``admin`` (everything), ``service`` (internal service-to-service and Pub/Sub push).
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field

from fastapi import Depends, HTTPException, Request

from convoiq.config import Settings, get_settings
from convoiq.logging import bind, get_logger

log = get_logger("convoiq.auth")


@dataclass(frozen=True)
class Principal:
    subject: str
    roles: frozenset[str] = field(default_factory=frozenset)
    tenant: str | None = None

    def has(self, *roles: str) -> bool:
        return "admin" in self.roles or any(r in self.roles for r in roles)


def _digest(k: str) -> str:
    return hashlib.sha256(k.encode()).hexdigest()


def parse_principals(spec: str) -> dict[str, frozenset[str]]:
    """``"a@x.iam:ingest|analyst,b@x.iam,carol@corp.com:approver"`` -> email -> roles (a bare email means the ``service`` role)."""
    out: dict[str, frozenset[str]] = {}
    for item in filter(None, (x.strip() for x in spec.split(","))):
        email, _, roles = item.partition(":")
        out[email.strip().lower()] = frozenset(r for r in roles.split("|") if r) or frozenset({"service"})
    return out


def parse_api_keys(spec: str) -> dict[str, tuple[str, frozenset[str]]]:
    out = {}
    for i, item in enumerate(filter(None, (x.strip() for x in spec.split(",")))):
        key, _, roles = item.partition(":")
        out[_digest(key)] = (f"apikey-{i}", frozenset(r for r in roles.split("|") if r))
    return out


class Authenticator:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()
        self.keys = parse_api_keys(self.s.api_keys)
        self.principals = parse_principals(self.s.allowed_service_accounts)

    def _bearer(self, request: Request) -> str | None:
        h = request.headers.get("authorization", "")
        if h.lower().startswith("bearer "):
            return h[7:].strip()
        return request.headers.get("x-api-key")

    def authenticate(self, request: Request) -> Principal:
        mode = self.s.auth_mode
        if mode == "none":
            return Principal("anonymous-dev", frozenset({"admin"}))
        token = self._bearer(request)
        if not token:
            raise HTTPException(401, "missing credentials", headers={"WWW-Authenticate": "Bearer"})
        if mode == "apikey":
            d = _digest(token)
            for stored, (sub, roles) in self.keys.items():
                if hmac.compare_digest(stored, d):
                    return Principal(sub, roles)
            raise HTTPException(401, "invalid credentials")
        if mode == "oidc":
            return self._oidc(token)
        raise HTTPException(500, "auth misconfigured")

    def _oidc(self, token: str) -> Principal:
        try:
            from google.auth.transport import requests as greq
            from google.oauth2 import id_token
            claims = id_token.verify_oauth2_token(token, greq.Request(), audience=self.s.oidc_audience)
        except Exception as e:  # signature, audience, expiry...
            log.warning("oidc verification failed", error=type(e).__name__)
            raise HTTPException(401, "invalid token") from e
        email = claims.get("email", "")
        if not claims.get("email_verified", False):
            raise HTTPException(401, "unverified identity")
        roles = self.principals.get(email.lower())
        if not roles:
            # IdP-issued tokens (Identity Platform / Workforce Identity) may carry a 'roles' claim; Google-signed tokens never do
            roles = frozenset(str(claims.get("roles", "")).split(",")) - {""}
        if not roles:
            raise HTTPException(403, "identity not authorised")
        return Principal(email, roles)


def get_authenticator(request: Request) -> Authenticator:
    return request.app.state.auth


def require(*roles: str):
    """FastAPI dependency factory enforcing at least one of ``roles`` (admin always passes)."""
    async def dep(request: Request, auth: Authenticator = Depends(get_authenticator)) -> Principal:
        p = auth.authenticate(request)
        if roles and not p.has(*roles):
            raise HTTPException(403, f"requires role: {'|'.join(roles)}")
        bind(principal=p.subject)
        request.state.principal = p
        return p
    return dep

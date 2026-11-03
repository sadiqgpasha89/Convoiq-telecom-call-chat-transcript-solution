"""Deterministic pseudonymisation tokens (HMAC-SHA256) - the "format-preserving token" of the spec.

``<PII_CARD_A94F21>`` is a *keyed* hash of the normalised value: the same card/phone always maps
to the same token (so repeat contacts and cross-channel sessions can be joined downstream) but the
value cannot be recovered or brute-forced without the key, which never leaves the sovereign zone.
"""
from __future__ import annotations

import hashlib
import hmac
import re

_NORMALISERS = {
    "PHONE": lambda v: re.sub(r"\D", "", v)[-10:],
    "CARD": lambda v: re.sub(r"\D", "", v),
    "SSN": lambda v: re.sub(r"\D", "", v),
    "IMEI": lambda v: re.sub(r"\D", "", v),
    "ACCOUNT": lambda v: re.sub(r"\D", "", v),
    "PIN": lambda v: re.sub(r"\D", "", v),
    "CVV": lambda v: re.sub(r"\D", "", v),
    "EMAIL": lambda v: v.strip().lower(),
    "NAME": lambda v: re.sub(r"\s+", " ", v.strip().lower()),
    "ADDRESS": lambda v: re.sub(r"[\s,.]+", " ", v.strip().lower()),
}
TOKEN_RE = re.compile(r"<PII_[A-Z0-9]+_[0-9A-F]{6,}>")


class Tokenizer:
    def __init__(self, key: bytes | str, token_len: int = 6):
        if isinstance(key, str):
            key = key.encode()
        if len(key) < 16:
            raise ValueError("tokenisation key must be at least 16 bytes")
        self._key = key
        self.n = token_len

    def normalise(self, typ: str, value: str) -> str:
        return _NORMALISERS.get(typ, lambda v: v.strip().lower())(value)

    def token(self, typ: str, value: str) -> str:
        digest = hmac.new(self._key, f"{typ}|{self.normalise(typ, value)}".encode(), hashlib.sha256).hexdigest()
        return f"<PII_{typ}_{digest[: self.n].upper()}>"

    def pseudo_id(self, value: str, namespace: str = "cust") -> str:
        """Stable pseudonymous customer id from an MSISDN / customer number."""
        norm = re.sub(r"\D", "", value)[-10:] or value.strip().lower()
        return hmac.new(self._key, f"{namespace}|{norm}".encode(), hashlib.sha256).hexdigest()[:20]

    def sign(self, content_sha256: str, interaction_id: str) -> str:
        return hmac.new(self._key, f"attest|{interaction_id}|{content_sha256}".encode(), hashlib.sha256).hexdigest()

    def verify(self, content_sha256: str, interaction_id: str, signature: str | None) -> bool:
        return bool(signature) and hmac.compare_digest(self.sign(content_sha256, interaction_id), signature or "")

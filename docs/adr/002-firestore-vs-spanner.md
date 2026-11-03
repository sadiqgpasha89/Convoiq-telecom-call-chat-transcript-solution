# ADR-002: Firestore for operational state; BigQuery for analytics
**Status:** accepted
**Decision:** Firestore (native) holds idempotency keys, status, approvals, the PII vault and the kill switch (TTL via `_expires_at`). BigQuery holds all facts/audit.
**Why:** Key-value access with TTL, strong consistency per document, no capacity planning. Spanner would add global consistency we do not need at ~10²–10³ writes/s.
**Consequences:** No cross-document transactions across services (we design for idempotent single-document updates). `FirestoreVault` currently uses the synchronous client inside async handlers – acceptable at current load, but should move to `AsyncClient` or a thread pool before high-volume use (KNOWN_LIMITS).

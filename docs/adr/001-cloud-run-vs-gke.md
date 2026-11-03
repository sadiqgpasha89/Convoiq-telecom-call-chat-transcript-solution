# ADR-001: Cloud Run (not GKE) for all services
**Status:** accepted
**Context:** Eight small, stateless HTTP services driven by Pub/Sub push; spiky traffic (outage storms); a small platform team.
**Decision:** Cloud Run with min-instances for latency-critical services (gateway, redaction), scale-to-zero for the rest, Direct VPC egress through Cloud NAT (static egress IP for partner allow-lists), internal ingress except the gateway behind the HTTPS LB + Cloud Armor.
**Why:** No cluster to patch; per-request autoscaling; native Pub/Sub push + OIDC; per-service service accounts.
**Consequences:** No GPUs (not needed – classical models, LLM via API). Long-running stateful work is awkward → the outage detector is pinned to one instance (ADR-005). If self-hosted LLM/GPU inference is ever required, add a GKE Autopilot pool behind an internal endpoint rather than moving everything.

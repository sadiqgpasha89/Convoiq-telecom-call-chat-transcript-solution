"""Churn agent: tabular propensity (IBM telco) fused with conversation evidence, then value-aware offer selection.

Stated-intent floors (0.35 cancel, 0.50 port-out request) keep explicit leavers out of the LOW band.
Fusion is done in logit space:  z = logit(p_tabular) + 3.0*conv_score + 0.8*frustration + 0.5*[sentiment worsening].
Coefficients are expert priors (documented in docs/MODELS.md) - the tabular model is trained, the conversation
increments are NOT learned from labelled churn conversations (none exist publicly); they are validated on the
synthetic churn scenarios only, so treat absolute probabilities as uncalibrated for conversations.
If no CRM profile is available the tabular prior falls back to the base rate and the output says so.
"""
from __future__ import annotations

import math
from typing import Any

from convoiq.agents.deps import AgentDeps
from convoiq.logging import get_logger
from convoiq.contracts import ChurnAssessment, ChurnDriver, EnrichedInteraction
from convoiq.models.classical import CHURN_DEFAULT_ROW
from convoiq.value import ASSUMPTIONS, best_offer, clv

log = get_logger("convoiq.churn")
MIN_PROFILE_FIELDS = 4
PROFILE_TO_ROW = {"tenure": "tenure", "contract": "Contract", "monthly_charges": "MonthlyCharges", "internet_service": "InternetService",
                  "payment_method": "PaymentMethod", "senior": "SeniorCitizen", "paperless": "PaperlessBilling",
                  "tech_support": "TechSupport", "online_security": "OnlineSecurity", "total_charges": "TotalCharges"}


def _logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def _num(x: Any) -> float | None:
    try:
        v = float(x)
        return v if math.isfinite(v) and v > 0 else None
    except (TypeError, ValueError):
        return None


def _sig(z: float) -> float:
    return 1 / (1 + math.exp(-z))


def profile_row(enr: EnrichedInteraction, ctx: dict[str, Any]) -> dict[str, Any]:
    prof = ctx.get("profile") or {}
    row = {PROFILE_TO_ROW[k]: v for k, v in prof.items() if k in PROFILE_TO_ROW and v is not None}
    for k, v in (enr.meta.extra or {}).items():
        if k in CHURN_DEFAULT_ROW:
            row[k] = v
    if enr.meta.tenure_months is not None and "tenure" not in row:
        row["tenure"] = enr.meta.tenure_months
    return row


def assess_churn(enr: EnrichedInteraction, deps: AgentDeps, ctx: dict[str, Any], identity_verified: bool | None = None) -> ChurnAssessment:
    s = deps.settings
    row = profile_row(enr, ctx)
    model = deps.bundle.churn
    drivers: list[ChurnDriver] = []
    have_profile = len(row) >= MIN_PROFILE_FIELDS     # sparse rows would be silently completed with training defaults -> misleading
    bad_profile = False
    base = None
    if model is not None and have_profile:
        try:
            base = float(model.predict_rows([row])[0])
            drivers += [ChurnDriver(feature=f, contribution=round(c, 4)) for f, c in model.explain(row, top=4)]
        except Exception as e:                 # CRM returned types/values the model cannot score -> degrade, never fail the interaction
            log.warning("churn profile unusable", error=type(e).__name__)
            bad_profile, base = True, None
    if base is None:
        base = float(getattr(model, "base_rate", 0.265)) if model is not None else 0.265
    sig = enr.churn
    sent = enr.sentiment
    inc_conv = 3.0 * sig.score
    inc_frus = 0.8 * sent.frustration_index
    inc_trend = 0.5 if sent.trend == "worsening" else 0.0
    z = _logit(base) + inc_conv + inc_frus + inc_trend
    risk = _sig(z)
    # Stated-intent floor (expert rule, not learned): an explicit cancellation / port-out request is an event, not a
    # probability - a favourable CRM profile (long tenure, 2-year contract) must not push such a customer below the
    # retention-desk threshold.  The floor only affects band + offer routing; the tabular model itself is untouched.
    floor = 0.50 if sig.port_out_request else 0.35 if (sig.cancel_intent and sig.score >= 0.5) else 0.0
    floored = floor > risk
    risk = round(max(risk, floor), 4)
    if inc_conv > 0:
        drivers.append(ChurnDriver(feature="conversation:" + (
            "port_out_request" if sig.port_out_request else "cancel_intent" if sig.cancel_intent else "competitor_or_price"),
            contribution=round(_sig(_logit(base) + inc_conv) - base, 4)))
    if inc_frus + inc_trend > 0.2:
        drivers.append(ChurnDriver(feature="conversation:frustration", contribution=round(_sig(_logit(base) + inc_frus + inc_trend) - base, 4)))
    if floored:
        drivers.append(ChurnDriver(feature="stated_intent_floor", contribution=round(risk - _sig(z), 4)))
    drivers.sort(key=lambda d: -abs(d.contribution))
    if not have_profile or bad_profile:
        drivers.append(ChurnDriver(feature="crm_profile_unavailable_or_partial(base_rate_prior)" if not bad_profile
                                   else "crm_profile_invalid(base_rate_prior)", contribution=0.0))
    band = "HIGH" if risk >= s.churn_high_threshold else "MEDIUM" if risk >= 0.35 else "LOW"

    mc = _num(row.get("MonthlyCharges") or (ctx.get("profile") or {}).get("monthly_charges"))
    contract = row.get("Contract") if isinstance(row.get("Contract"), str) else None
    value = clv(mc, contract)
    offer = None
    if band in ("HIGH", "MEDIUM") and identity_verified is not False:
        offer = best_offer(risk, mc, contract, s.retention_discount_max_pct)
        if offer is not None:
            offer = {**offer, "competitor": sig.competitor_mentions[0] if sig.competitor_mentions else None}
    out = ChurnAssessment(risk_score=risk, band=band, drivers=drivers[:5],
                          competitor=sig.competitor_mentions[0] if sig.competitor_mentions else None,
                          recommended_offer=offer, expected_value_at_risk_usd=round(risk * value, 2))
    return out

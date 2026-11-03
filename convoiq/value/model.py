"""Business-value model: turns churn risk + offers into dollars.

Every number that is an *assumption* (not learned from data) lives in ``ASSUMPTIONS`` so Finance can
review/override it (``CONVOIQ_VALUE_ASSUMPTIONS`` JSON env, see config) and so reports can print them.
Nothing here is presented as a measured uplift: it is expected value under stated assumptions; real
uplift must come from an A/B test (docs/VALUE_MODEL.md).
"""
from __future__ import annotations

import json
import os
from typing import Any

ASSUMPTIONS: dict[str, Any] = {
    "gross_margin": 0.55,                       # share of ARPU that is contribution margin
    "max_lifetime_months": 36,                  # horizon cap for CLV (no infinite tails)
    "monthly_hazard": {"Month-to-month": 0.035, "One year": 0.012, "Two year": 0.006},
    "default_monthly_charge": 65.0,
    "default_contract": "Month-to-month",
    "offer_months": 6,
    "offer_ladder_pct": [5, 10, 15, 20, 25],
    "accept_base": 0.12,                         # P(accept) at 0% for an at-risk customer
    "accept_per_pct": 0.014,
    "accept_cap": 0.6,
    "agent_cost_per_minute_usd": 0.90,
    "fraud_loss_per_sim_swap_usd": 1200.0,
}
if os.getenv("CONVOIQ_VALUE_ASSUMPTIONS"):
    ASSUMPTIONS.update(json.loads(os.environ["CONVOIQ_VALUE_ASSUMPTIONS"]))


def expected_lifetime_months(contract: str | None = None, tenure_months: int | None = None) -> float:
    h = ASSUMPTIONS["monthly_hazard"].get(contract or ASSUMPTIONS["default_contract"], 0.035)
    # geometric lifetime truncated at the horizon: sum_{k<H} (1-h)^k
    H = ASSUMPTIONS["max_lifetime_months"]
    return (1 - (1 - h) ** H) / h


def clv(monthly_charge: float | None, contract: str | None = None, tenure_months: int | None = None) -> float:
    mc = monthly_charge if monthly_charge else ASSUMPTIONS["default_monthly_charge"]
    return round(mc * ASSUMPTIONS["gross_margin"] * expected_lifetime_months(contract, tenure_months), 2)


def accept_prob(pct: float) -> float:
    return min(ASSUMPTIONS["accept_cap"], ASSUMPTIONS["accept_base"] + ASSUMPTIONS["accept_per_pct"] * pct)


def offer_economics(risk: float, monthly_charge: float, contract: str | None, pct: float) -> dict[str, float]:
    months = ASSUMPTIONS["offer_months"]
    p_acc = accept_prob(pct)
    value = clv(monthly_charge, contract)
    benefit = risk * p_acc * value                         # churn avoided among those who would leave
    cost = p_acc * (pct / 100.0) * monthly_charge * months  # discount also reaches accepters who would have stayed
    return {"pct": pct, "months": months, "p_accept": round(p_acc, 3), "benefit_usd": round(benefit, 2),
            "cost_usd": round(cost, 2), "ev_usd": round(benefit - cost, 2)}


def best_offer(risk: float, monthly_charge: float | None, contract: str | None, max_pct: float) -> dict[str, float] | None:
    mc = monthly_charge or ASSUMPTIONS["default_monthly_charge"]
    cands = [offer_economics(risk, mc, contract, p) for p in ASSUMPTIONS["offer_ladder_pct"] if p <= max_pct]
    cands = [c for c in cands if c["ev_usd"] > 0]
    return max(cands, key=lambda c: c["ev_usd"]) if cands else None

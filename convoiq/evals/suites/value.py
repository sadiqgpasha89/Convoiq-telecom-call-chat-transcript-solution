"""Business-value roll-up over a synthetic cohort.  EXPECTED values under the stated assumptions (value/model.py) - not measured uplift."""
from __future__ import annotations

import asyncio

from convoiq.evals.context import EvalContext
from convoiq.pipeline import corpus_to_raw
from convoiq.value import ASSUMPTIONS


def run(ctx: EvalContext) -> dict:
    convs = ctx.synth(n=500, seed=ctx.seed + 61)
    loop = asyncio.new_event_loop()
    try:
        outs = [loop.run_until_complete(ctx.pipeline.process(corpus_to_raw(c))) for c in convs]
    finally:
        loop.close()
    var = sum((o.analysis.churn.expected_value_at_risk_usd or 0) for o in outs if o.analysis.churn and o.analysis.churn.band in ("HIGH", "MEDIUM"))
    ev = sum(o.analysis.value_estimate.get("retention_expected_value_usd", 0) for o in outs)
    leak = sum(o.analysis.value_estimate.get("credit_leakage_flagged_usd", 0) for o in outs)
    credits = sum(a.amount_usd or 0 for o in outs for a in o.analysis.proposed_actions if a.type.value == "BSS_CREDIT")
    n_off = sum(any(a.type.value == "RETENTION_OFFER" for a in o.analysis.proposed_actions) for o in outs)
    return {"cohort_size": len(outs), "revenue_at_risk_usd_high_medium_churn": round(var, 2), "retention_offers_proposed": n_off,
            "retention_expected_value_usd": round(ev, 2), "unauthorized_credit_flagged_usd": round(leak, 2),
            "credits_proposed_usd": round(credits, 2), "assumptions": ASSUMPTIONS,
            "note": "expected values under stated assumptions on synthetic data; validate with an A/B test before budgeting"}

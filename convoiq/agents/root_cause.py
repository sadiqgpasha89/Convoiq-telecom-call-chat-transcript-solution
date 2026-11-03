"""Root-cause agent: calibrated ML hypotheses x case-based reasoning x KB grounding x BSS/OSS verification.

Evidence rules
* ML prob is the prior.  Similar resolved cases (LSA nearest neighbours) vote and move it 30 %.
* BSS/OSS context can **confirm** (adds evidence, +prob) or **contradict**.  A contradiction only *rejects*
  when the context source is authoritative; otherwise it downgrades (tau2's static DB, stale caches).
* An LLM may only break near-ties, and only by picking from the closed candidate set.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from convoiq.agents.deps import AgentDeps
from convoiq.agents.llm import LLMUnavailable, wrap_untrusted
from convoiq.contracts import Diagnosis, EnrichedInteraction, Evidence, Hypothesis
from convoiq.models.features import customer_view
from convoiq.nlp.text import tool_tokens
from convoiq.taxonomy import ROOT_CAUSES

MIN_PRIMARY_PROB = 0.25


class RootCausePick(BaseModel):
    tag: str
    rationale: str = ""


def _first_amount(vals: list[str]) -> float | None:
    for v in vals:
        try:
            return float(v.replace(",", ""))
        except ValueError:
            continue
    return None


def verify_with_context(tag: str, ctx: dict[str, Any], enr: EnrichedInteraction) -> tuple[str | None, list[str], str]:
    """-> (confirmed|contradicted|None, evidence refs, note)."""
    profile, bills, tower = ctx.get("profile"), ctx.get("bills"), ctx.get("tower")
    refs = ctx.get("refs", [])
    if tag == "network_tower_outage" and tower is not None:
        down = str(tower.get("status", "")).lower() in {"down", "degraded", "outage"}
        return ("confirmed" if down else "contradicted", [r for r in refs if r.startswith("oss:")], f"tower status={tower.get('status')}")
    if tag in ("overdue_bill_suspension", "contract_end_suspension") and profile is not None:
        susp = str(profile.get("line_status", "")).lower() == "suspended"
        return ("confirmed" if susp else "contradicted", [r for r in refs if r.startswith("bss:customer")], f"line_status={profile.get('line_status')}")
    if tag.startswith("billing_") and bills:
        claimed = _first_amount(enr.entities.get("amounts_usd", []))
        amts = [float(b.get("amount", 0)) for b in bills]
        if claimed is not None and any(abs(a - claimed) < 0.01 for a in amts):
            return "confirmed", [r for r in refs if r.startswith("bss:bill")], f"bill contains charge ${claimed:.2f}"
        if tag == "billing_duplicate_charge" and len(amts) != len(set(amts)):
            return "confirmed", [r for r in refs if r.startswith("bss:bill")], "duplicate amounts on bills"
        return "contradicted", [r for r in refs if r.startswith("bss:bill")], "no matching charge on bills"
    if tag == "data_usage_exceeded" and profile and "data_used_gb" in profile and "data_limit_gb" in profile:
        over = profile["data_used_gb"] >= profile["data_limit_gb"]
        return ("confirmed" if over else "contradicted", [r for r in refs if r.startswith("bss:customer")],
                f"used {profile['data_used_gb']}/{profile['data_limit_gb']} GB")
    if tag.startswith("user_abroad_roaming") and profile and "roaming_enabled" in profile:
        want_enabled_on_line = tag in ("user_abroad_roaming_enabled_off",)
        ok = bool(profile["roaming_enabled"]) == want_enabled_on_line
        return ("confirmed" if ok else "contradicted", [r for r in refs if r.startswith("bss:customer")],
                f"line roaming_enabled={profile['roaming_enabled']}")
    return None, [], ""


NETWORK_SYMPTOMS = {"NO_SERVICE", "MOBILE_DATA", "MMS"}
TOWER_SEED_PRIOR = 0.30


async def diagnose(enr: EnrichedInteraction, deps: AgentDeps, ctx: dict[str, Any], llm=None) -> Diagnosis:
    hyp: dict[str, float] = {r.tag: r.prob for r in enr.root_causes}
    ev: dict[str, list[Evidence]] = {t: [Evidence(kind="model", ref=f"model:rootcause@{deps.bundle.versions.get('rootcause', 'n/a').split('@')[-1]}")]
                                     for t in hyp}
    qtext = customer_view(enr.turns) + " " + tool_tokens(enr.turns)

    # --- case-based reasoning ---------------------------------------------------------
    cases, fixes_vote = [], []
    if deps.bundle.case_index is not None:
        from convoiq.rag.cases import CaseIndex
        cases = deps.bundle.case_index.search(qtext, k=7, issue_type=enr.issue.issue_type)
        n = max(len(cases), 1)
        share: dict[str, float] = {}
        for c in cases:
            for t in set(c.root_causes):
                share[t] = share.get(t, 0.0) + 1.0 / n
        for t, sh in share.items():
            old = hyp.get(t, 0.0)
            hyp[t] = 0.7 * old + 0.3 * sh
            ev.setdefault(t, []).append(Evidence(kind="model", ref="model:case_index", quote=None))
        fixes_vote = CaseIndex.vote_fixes(cases)

    # --- context-seeded hypotheses ------------------------------------------------------------
    # Authoritative OSS evidence may point at a cause the text-based prior ranked low (customers rarely say "the tower is down").
    # Seed it with a modest prior so it must still win on confirmation; never from non-authoritative or missing context.
    tower = ctx.get("tower")
    if (tower is not None and ctx.get("authoritative") and enr.issue.issue_type in NETWORK_SYMPTOMS
            and str(tower.get("status", "")).lower() in {"down", "degraded", "outage"}):
        hyp["network_tower_outage"] = max(hyp.get("network_tower_outage", 0.0), TOWER_SEED_PRIOR)
        ev.setdefault("network_tower_outage", [Evidence(kind="model", ref="context:oss_tower_status")])

    # --- KB grounding + context verification ---------------------------------------------
    kb_ids: set[str] = set()
    hyps: list[Hypothesis] = []
    notes: dict[str, str] = {}
    for tag, p in sorted(hyp.items(), key=lambda x: -x[1])[:6]:
        if p < 0.05:
            continue
        label, domain = ROOT_CAUSES.get(tag, (tag, "UNKNOWN"))
        hits = deps.retriever.retrieve(f"{label} {tag.replace('_', ' ')}", k=1)
        evid = list(ev.get(tag, []))
        for h in hits:
            evid.append(Evidence(kind="kb", ref=h.chunk.id))
            kb_ids.add(h.chunk.id)
        status = "plausible"
        verdict, refs, note = verify_with_context(tag, ctx, enr)
        if verdict == "confirmed":
            status, p = "confirmed", min(0.99, p + 0.25)
            evid += [Evidence(kind="tool", ref=r) for r in refs]
            notes[tag] = note
        elif verdict == "contradicted":
            p *= 0.6
            evid += [Evidence(kind="tool", ref=r) for r in refs]
            notes[tag] = note
            if ctx.get("authoritative"):
                status = "rejected"
        elif p >= 0.5:
            status = "plausible"
        hyps.append(Hypothesis(tag=tag, label=label, domain=domain, prob=round(p, 4), status=status, evidence=evid))
    hyps.sort(key=lambda h: (h.status == "rejected", -h.prob))
    live = [h for h in hyps if h.status != "rejected"]

    ctx_out: dict[str, Any] = {"context_notes": notes, "n_similar_cases": len(cases), "kb_ids": sorted(kb_ids),
                               "context_errors": ctx.get("errors", []), "context_authoritative": ctx.get("authoritative", False)}

    # --- LLM only breaks near ties, from the closed set -----------------------------------------------
    if len(live) >= 2 and live[0].prob - live[1].prob < 0.12 and llm is not None and getattr(llm, "available", False):
        cand = [h.tag for h in live[:3]]
        try:
            pick = await llm.generate_json(
                "Choose the single most likely root cause from the CANDIDATES list for this telecom support conversation. "
                f"CANDIDATES: {cand}. Return JSON {{\"tag\": <one candidate>, \"rationale\": <one sentence>}}.",
                wrap_untrusted("conversation", qtext[:2500]), RootCausePick, 200)
            if pick.tag in cand:
                live.sort(key=lambda h: h.tag != pick.tag)
                ctx_out["llm_tiebreak"] = {"picked": pick.tag, "candidates": cand}
        except LLMUnavailable as e:
            ctx_out["llm_tiebreak_skipped"] = str(e)

    primary = live[0] if live and live[0].prob >= MIN_PRIMARY_PROB else None
    fixes: list[str] = []
    if primary:
        fixes += deps.remediation.get(primary.tag, [])
        fixes += [f for f, _ in fixes_vote if f not in fixes]
    return Diagnosis(primary_tag=primary.tag if primary else None, primary_label=primary.label if primary else None,
                     owner_domain=primary.domain if primary else None, confidence=primary.prob if primary else 0.0,
                     hypotheses=hyps, recommended_fixes=fixes[:6], context=ctx_out)

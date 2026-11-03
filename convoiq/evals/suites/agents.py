"""Agent-mesh quality: root-cause agent vs gold, tool/fix selection (TSA), action planning, grounding, graceful degradation."""
from __future__ import annotations

import asyncio
from collections import Counter

import numpy as np

from convoiq.agents.llm import ScriptedLLM
from convoiq.contracts import AnalysisMode
from convoiq.evals.context import EvalContext
from convoiq.evals.metrics import tool_selection_prf, trajectory_efficiency_ratio
from convoiq.pipeline import LocalPipeline, corpus_to_raw
from convoiq.training.datasets import prep


def _run(loop, coro):
    return loop.run_until_complete(coro)


def run(ctx: EvalContext) -> dict:
    res: dict = {}
    loop = asyncio.new_event_loop()
    pl = ctx.pipeline
    try:
        # ---------------- tau2 test: root cause + fix selection (no BSS/OSS context: tau2 DB cannot be aligned per task) ----------
        t2 = ctx.tau2_test
        top1 = anyhit = 0
        setf1 = 0.0
        gold_actions_all, rec_all = [], []
        P = R = F = 0.0
        ter, ident_first, eff, n_flags = [], [], [], 0
        faith, claims_n, claims_ok = [], 0, 0
        lat = []
        for c in t2:
            r = _run(loop, pl.process(corpus_to_raw(c)))
            a = r.analysis
            gold = set(c["root_causes"])
            tag = a.diagnosis.primary_tag if a.diagnosis else None
            top1 += tag in gold
            live = {h.tag for h in (a.diagnosis.hypotheses if a.diagnosis else []) if h.status != "rejected" and h.prob >= 0.3}
            anyhit += bool(gold & live)
            hi = {h.tag for h in (a.diagnosis.hypotheses if a.diagnosis else []) if h.status != "rejected" and h.prob >= 0.5}
            setf1 += 2 * len(gold & hi) / max(len(gold) + len(hi), 1)
            gold_names = [x["name"] for x in c["gold"]["assistant_actions"] + c["gold"]["user_actions"]]
            rec = [f.split(":", 1)[1] for f in (a.diagnosis.recommended_fixes if a.diagnosis else [])]
            m = tool_selection_prf(gold_names, rec)
            P, R, F = P + m["precision"], R + m["recall"], F + m["f1"]
            n_actual = sum(len(t.tool_calls) for t in prep(c) if t.role.value in ("agent", "assistant")) + sum(len(t.tool_calls) for t in prep(c) if t.role.value == "customer")
            if c["outcome"]["resolved"] and gold_names:
                ter.append(trajectory_efficiency_ratio(len(gold_names), max(n_actual, 1)))
            if a.qa and a.qa.efficiency is not None:
                eff.append(a.qa.efficiency)
                ident_first.append(a.qa.identity_checked_first)
            n_flags += bool(a.qa and a.qa.policy_flags)
            faith.append(a.faithfulness or 0)
            claims_n += len(a.claims); claims_ok += sum(cl.attributed for cl in a.claims)
            lat.append(a.latency_ms)
        n = len(t2)
        res.update({"tau2_rootcause_top1_in_gold": top1 / n, "tau2_rootcause_any_hit": anyhit / n, "tau2_rootcause_set_f1_at_0.5": setf1 / n,
                    "tau2_fix_selection_precision": P / n, "tau2_fix_selection_recall": R / n, "tau2_fix_selection_f1": F / n,
                    "tau2_human_agent_trajectory_efficiency_ratio_mean": float(np.mean(ter)) if ter else None,
                    "tau2_qa_policy_flag_rate": n_flags / n,
                    "tau2_faithfulness_mean": float(np.mean(faith)), "tau2_citation_validity": claims_ok / max(claims_n, 1)})

        # ---------------- synthetic with seeded authoritative BSS/OSS facts -----------------------------------------------------------------
        syn = ctx.synth(n=max(ctx.n_syn, 400), seed=ctx.seed + 21)
        exp_tp, exp_fp, exp_fn = Counter(), Counter(), Counter()
        rc_ok = rc_n = confirmed = 0
        faith_s = []
        degraded = 0
        results = []
        for c in syn:
            r = _run(loop, pl.process(corpus_to_raw(c)))
            results.append((c, r))
        for c, r in results:
            a = r.analysis
            lab = c["labels"]
            got = {x.type.value for x in a.proposed_actions}
            exp = set(lab["expected_actions"])
            if "R1_NO_ID_VERIFICATION" in lab["violations"]:
                exp.discard("RETENTION_OFFER")          # policy: no account-specific offers to unverified callers
            if lab["fraud"] or lab["injection"]:
                exp = {"ESCALATE_FRAUD"} if lab["fraud"] else {"ESCALATE_HUMAN"}
                got = got & {"ESCALATE_FRAUD", "ESCALATE_HUMAN"}
            for t in exp | got:
                if t == "CRM_NOTE":
                    continue
                exp_tp[t] += t in exp and t in got
                exp_fp[t] += t in got and t not in exp
                exp_fn[t] += t in exp and t not in got
            if c["root_causes"] and a.diagnosis:
                rc_n += 1
                rc_ok += a.diagnosis.primary_tag in c["root_causes"]
                confirmed += any(h.status == "confirmed" for h in a.diagnosis.hypotheses)
            faith_s.append(a.faithfulness or 0)
            degraded += a.degraded
        for t in sorted(set(exp_tp) | set(exp_fp) | set(exp_fn)):
            p = exp_tp[t] / max(exp_tp[t] + exp_fp[t], 1); rc = exp_tp[t] / max(exp_tp[t] + exp_fn[t], 1)
            res[f"synth_action_{t}_precision"], res[f"synth_action_{t}_recall"] = p, rc
        tp, fp, fn = sum(exp_tp.values()), sum(exp_fp.values()), sum(exp_fn.values())
        res["synth_action_micro_f1"] = 2 * tp / max(2 * tp + fp + fn, 1)
        res["synth_rootcause_top1"] = rc_ok / max(rc_n, 1)
        res["synth_rootcause_context_confirmed_rate"] = confirmed / max(rc_n, 1)
        res["synth_faithfulness_mean"] = float(np.mean(faith_s))
        res["synth_degraded_rate"] = degraded / len(results)

        # ---------------- graceful degradation: backend down / slow ---------------------------------------------------------------------
        ctx.tools.fail_with = "timeout"
        try:
            sub = [c for c, _ in results][:80]
            outs = [_run(loop, pl.process(corpus_to_raw(c))) for c in sub]
        finally:
            ctx.tools.fail_with = None
        res["backend_down_completion_rate"] = float(np.mean([bool(o.analysis.summary) for o in outs]))
        res["backend_down_still_proposes_note_rate"] = float(np.mean([any(x.type.value == "CRM_NOTE" for x in o.analysis.proposed_actions) for o in outs]))
        res["backend_down_rootcause_top1"] = float(np.mean([bool(o.analysis.diagnosis and o.analysis.diagnosis.primary_tag in (c["root_causes"] or [o.analysis.diagnosis.primary_tag]))
                                                            for o, c in zip(outs, sub)]))

        # ---------------- hallucination control with a hostile / sloppy LLM ----------------------------------------------------------------
        bad = {"summary": "The customer was promised a $900 refund and a free phone, as confirmed by supervisor Dana.",
               "claims": [{"text": "Supervisor approved a $900 refund", "evidence": [{"kind": "turn", "ref": "turn:999", "quote": "refund approved"}]},
                          {"text": "Customer is a VIP", "evidence": []}]}
        liar = LocalPipeline(ctx.settings, ctx.bundle, tools=ctx.tools, llm=ScriptedLLM([bad] * 200))
        liar.deps.remediation = ctx.train_remediation
        fell_back = used_llm = 0
        for c in [c for c, _ in results if c["labels"]["family"] in ("billing_dispute", "plan_change")][:40]:
            o = _run(loop, liar.process(corpus_to_raw(c)))
            fell_back += o.analysis.llm.get("summary_source") == "template"
            used_llm += o.analysis.llm.get("summary_source") == "llm_verified"
            assert "$900" not in o.analysis.summary
        res["hostile_llm_fallback_rate"] = fell_back / max(fell_back + used_llm, 1)
        # a well-behaved LLM that cites real evidence is accepted
        good = lambda s, u: {"summary": "Customer disputed a billing charge and the agent applied a credit after verification.",
                             "claims": [{"text": "Customer disputed a charge", "evidence": [{"kind": "turn", "ref": "turn:1"}]}]}
        ok = LocalPipeline(ctx.settings, ctx.bundle, tools=ctx.tools, llm=ScriptedLLM([good] * 50))
        ok.deps.remediation = ctx.train_remediation
        c0 = next(c for c, _ in results if c["labels"]["family"] == "billing_dispute")
        o = _run(loop, ok.process(corpus_to_raw(c0)))
        res["wellformed_llm_accepted"] = float(o.analysis.llm.get("summary_source") == "llm_verified")
    finally:
        loop.close()
    return res

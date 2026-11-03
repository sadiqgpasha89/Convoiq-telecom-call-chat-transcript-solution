"""Retrieval quality over the tau2 policy / troubleshooting KB.  Relevance is WEAK supervision: a chunk is relevant to a ticket if it
mentions any step that the gold solution actually performs.  Queries are held-out (test split) ticket texts."""
from __future__ import annotations

import gzip
import json

from convoiq.config import ROOT
from convoiq.evals.context import EvalContext
from convoiq.evals.metrics import mrr, recall_at_k
from convoiq.rag.retriever import HybridRetriever
from convoiq.training.datasets import tau2_split

ACTION_PHRASES = {
    "toggle_airplane_mode": ["airplane mode"], "toggle_data": ["mobile data"], "toggle_roaming": ["roaming"], "enable_roaming": ["roaming"],
    "toggle_data_saver_mode": ["data saver"], "set_network_mode_preference": ["network mode"], "check_network_mode_preference": ["network mode"],
    "reset_apn_settings": ["apn"], "reseat_sim_card": ["sim card"], "disconnect_vpn": ["vpn"], "toggle_wifi_calling": ["wi-fi calling", "wifi calling"],
    "grant_app_permission": ["permission"], "resume_line": ["suspended", "resume"], "send_payment_request": ["overdue", "payment"],
    "refuel_data": ["refuel"], "reboot_device": ["restart", "reboot"], "check_status_bar": ["status bar"], "run_speed_test": ["speed test"],
    "check_network_status": ["network status"], "can_send_mms": ["mms"], "make_payment": ["payment"],
}


def run(ctx: EvalContext) -> dict:
    p = ROOT / "data" / "corpus" / "tau2_telecom_tickets.jsonl.gz"
    test_ids = tau2_split()["test"]
    tickets = [t for t in (json.loads(l) for l in gzip.open(p, "rt")) if t["ticket_id"] in test_ids and t["root_causes"]]
    kb = HybridRetriever(embedder=ctx.bundle.embedder)
    res: dict = {"n_queries": 0}
    stats = {m: {"r1": [], "r3": [], "r5": [], "mrr": []} for m in ("lexical", "dense", "hybrid")}
    # troubleshooting knowledge lives in the manual AND the workflow doc; the policy doc is not a troubleshooting source
    manual = [c for c in kb.chunks if c.doc in ("tech_support_manual", "tech_support_workflow")]
    for t in tickets:
        q = (t["text"] or t["reason_for_call"] or "").strip()
        phrases = {ph for a in t["gold_actions"] for ph in ACTION_PHRASES.get(a["name"], [])}
        if not q or not phrases:
            continue
        rel = {c.id for c in manual if any(ph in c.text.lower() for ph in phrases)}
        if not rel:
            continue
        res["n_queries"] += 1
        for mode in stats:
            ids = [h.chunk.id for h in kb.retrieve(q, k=10, mode=mode)]
            stats[mode]["r1"].append(float(bool(set(ids[:1]) & rel)))
            stats[mode]["r3"].append(float(bool(set(ids[:3]) & rel)))
            stats[mode]["r5"].append(float(bool(set(ids[:5]) & rel)))
            stats[mode]["mrr"].append(mrr(rel, ids))
    for mode, d in stats.items():
        for k, v in d.items():
            res[f"{mode}_{k}"] = sum(v) / max(len(v), 1)
    res["hybrid_minus_lexical_mrr"] = res["hybrid_mrr"] - res["lexical_mrr"]
    return res

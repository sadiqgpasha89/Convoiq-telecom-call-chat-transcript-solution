"""Trajectory QA: audits *how* the agent (human CSR or bot) worked, using only the transcript + tool calls.

Metrics follow the tau-bench/spec vocabulary: redundant calls, repeated failures, transfer behaviour and a
trajectory-efficiency estimate (unique successful calls / total calls).  Policy flags are tau2-telecom
policy checks that are verifiable from the log alone.
"""
from __future__ import annotations

import json

from convoiq.agents.dialogue import LOOKUP_TOOLS, MUTATING_TOOLS, TRANSFER_TOOLS, is_agent, verification_index
from convoiq.contracts import EnrichedInteraction, Speaker, TrajectoryQA


def audit_trajectory(enr: EnrichedInteraction) -> TrajectoryQA:
    calls = []        # (turn, name, args_json)
    for t in enr.turns:
        if is_agent(t):
            for tc in t.tool_calls:
                calls.append((t.i, tc.name, json.dumps(tc.arguments, sort_keys=True, default=str)))
    n_errors = sum(1 for t in enr.turns if t.role == Speaker.TOOL and (t.is_error or (t.text or "").lower().startswith("error")))
    seen, redundant = set(), 0
    for _, name, args in calls:
        key = (name, args)
        if key in seen and name not in MUTATING_TOOLS:
            redundant += 1
        seen.add(key)
    # repeated failure: an errored tool result followed by the *same* call again
    repeated_failures = 0
    last_call = None
    for t in enr.turns:
        if is_agent(t) and t.tool_calls:
            cur = [(tc.name, json.dumps(tc.arguments, sort_keys=True, default=str)) for tc in t.tool_calls]
            if last_call is not None and last_call[1] and any(c in last_call[0] for c in cur):
                repeated_failures += 1
            last_call = (cur, False)
        elif t.role == Speaker.TOOL and last_call is not None:
            last_call = (last_call[0], bool(t.is_error or (t.text or "").lower().startswith("error")))
    flags: list[str] = []
    names = [c[1] for c in calls]
    transfers = sum(n in TRANSFER_TOOLS for n in names)
    diag = [n for n in names if n not in TRANSFER_TOOLS]
    if transfers and not diag:
        flags.append("transfer_without_any_diagnostic_step")
    if transfers > 1:
        flags.append("multiple_transfers")
    first_mut = next((i for i, n, _ in calls if n in MUTATING_TOOLS), None)
    ver = verification_index(enr.turns)
    ident_first = None if first_mut is None else (ver is not None and ver <= first_mut)
    if ident_first is False:
        flags.append("mutating_call_before_identification")
    if repeated_failures >= 2:
        flags.append("retry_loop")
    if redundant >= 3:
        flags.append("many_redundant_calls")
    # tau2 policy: do not offer/perform data refuel without confirming charge (check "refuel" w/o user yes) - log-checkable subset
    for t in enr.turns:
        if is_agent(t) and any(tc.name == "refuel_data" for tc in t.tool_calls):
            prev_cust = next((x for x in reversed(enr.turns[: enr.turns.index(t)]) if x.role == Speaker.CUSTOMER), None)
            if prev_cust is None or not any(w in (prev_cust.text or "").lower() for w in ("yes", "ok", "sure", "go ahead", "please", "agree", "fine", "proceed")):
                flags.append("refuel_without_customer_consent")
                break
    n = len(calls)
    useful = len({(c[1], c[2]) for c in calls}) - sum(1 for t in enr.turns if t.role == Speaker.TOOL and t.is_error)
    return TrajectoryQA(n_tool_calls=n, n_errors=n_errors, redundant_calls=redundant, repeated_failures=repeated_failures,
                        identity_checked_first=ident_first, transfers=transfers, policy_flags=flags,
                        efficiency=round(max(0.0, useful) / n, 3) if n else None)

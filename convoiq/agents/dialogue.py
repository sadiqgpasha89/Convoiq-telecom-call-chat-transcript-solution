"""Dialogue-structure helpers shared by compliance and QA agents."""
from __future__ import annotations

import re

from convoiq.contracts import Speaker, Turn

LOOKUP_TOOLS = {"get_customer_by_phone", "get_customer_by_id", "get_customer_by_name", "lookup_customer", "verify_customer"}
MUTATING_TOOLS = {"refuel_data", "send_payment_request", "resume_line", "enable_roaming", "disable_roaming", "suspend_line",
                  "apply_credit", "change_plan", "activate_esim", "swap_sim", "port_out"}
TRANSFER_TOOLS = {"transfer_to_human_agents", "transfer_to_agent", "escalate"}

VERIFY_ASK = re.compile(r"\b(pin|passcode|date of birth|dob|birth ?date|security (?:question|code|word)|verify|verification|"
                        r"last (?:four|4)|confirm (?:your|the) (?:identity|name|address|pin))\b", re.I)
VERIFIED_SAYS = re.compile(r"\b(you'?re verified|identity (?:is )?confirmed|thanks? for verifying|verified you|i'?ve verified)\b", re.I)
SECRET_TOKEN = re.compile(r"<PII_(PIN|DOB|SSN|ACCOUNT)_[0-9A-F]+>")
ACCOUNT_ACTION = re.compile(
    r"(pull(?:ing)? up your account|open(?:ing)? your account|\bi (?:can )?see (?:the|your|a|an) (?:charge|bill|account|balance|invoice|plan)\b|"
    r"\bi'?ve (?:applied|credited|updated|changed|activated|sent|resumed|enabled|switched|moved)\b|"
    r"\bi'?ll (?:credit|apply|activate|send|update|change|enable|switch)\b|\byour (?:balance|bill) is\b|\byour account (?:shows|has|is)\b)", re.I)
TRANSFER_OFFER = re.compile(r"\b(transfer|supervisor|manager|escalat\w*|specialist|human agent|senior|tier ?2|call ?back)\b", re.I)


def is_agent(t: Turn) -> bool:
    return t.role in (Speaker.AGENT, Speaker.BOT)


def agent_lookup_index(turns: list[Turn]) -> int | None:
    for t in turns:
        if is_agent(t) and any(tc.name in LOOKUP_TOOLS for tc in t.tool_calls):
            return t.i
    return None


def verification_index(turns: list[Turn]) -> int | None:
    """Index of the first turn at which identity was established (None if never)."""
    for pos, t in enumerate(turns):
        if is_agent(t) and any(tc.name in LOOKUP_TOOLS for tc in t.tool_calls):
            return t.i
        if is_agent(t) and VERIFIED_SAYS.search(t.text or ""):
            return t.i
        if is_agent(t) and VERIFY_ASK.search(t.text or ""):
            for u in turns[pos + 1: pos + 4]:
                if u.role == Speaker.CUSTOMER and SECRET_TOKEN.search(u.text or ""):
                    return u.i
    return None


def first_account_action(turns: list[Turn]) -> tuple[int, str] | None:
    for t in turns:
        if not is_agent(t):
            continue
        if any(tc.name in MUTATING_TOOLS for tc in t.tool_calls):
            return t.i, next(tc.name for tc in t.tool_calls if tc.name in MUTATING_TOOLS)
        m = ACCOUNT_ACTION.search(t.text or "")
        if m:
            return t.i, m.group(0)
    return None

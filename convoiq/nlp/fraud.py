"""Social-engineering / SIM-swap indicators from customer turns (high-precision rules; model score is fused in enrichment)."""
from __future__ import annotations

import re

from convoiq.contracts import Turn
from convoiq.nlp.text import customer_turns

BYPASS = re.compile(r"\b(without (?:any )?(?:verif\w+|verifying|id|pin)|skip (?:the )?(?:verif\w+|security)|can'?t verify|cannot verify|"
                    r"disable (?:the )?verif\w+|no need (?:for|to) verif\w+|don'?t have (?:my )?(?:pin|id))\b", re.I)
URGENCY = re.compile(r"\b(urgent\w*|emergency|right now|immediately|hurry|asap|as soon as possible)\b", re.I)
IMPERSON = re.compile(r"\b(this is (?:the )?(?:carrier|network|security|telco|technical|it) (?:team|department|support)|i'?m (?:from|with) "
                      r"(?:the )?(?:carrier|network|your company|security|head office)|security team)\b", re.I)
SIM_CHANGE = re.compile(r"\b((?:e-?sim|sim)\b[^.!?]{0,25}\b(?:swap|transfer|move|activate|change|replace)|(?:swap|transfer|move)\b[^.!?]{0,25}\b(?:e-?sim|sim)\b|"
                        r"new (?:sim|device|phone)\b|transfer the line|port (?:my|this) number)", re.I)
STOLEN = re.compile(r"\b(stolen|lost my phone|phone (?:was )?(?:stolen|lost))\b", re.I)


def fraud_signals(turns: list[Turn]) -> tuple[float, list[str]]:
    text = " \n ".join(t.text for t in customer_turns(turns))
    f = {n: bool(rx.search(text)) for n, rx in
         [("bypass_verification", BYPASS), ("urgency", URGENCY), ("impersonation", IMPERSON), ("sim_change", SIM_CHANGE), ("stolen_device", STOLEN)]}
    flags = [k for k, v in f.items() if v]
    score = 0.0
    if f["impersonation"] and (f["bypass_verification"] or f["sim_change"]):
        score = 0.9
    elif f["sim_change"] and f["bypass_verification"]:
        score = 0.85
    elif f["sim_change"] and (f["urgency"] or f["stolen_device"]):
        score = 0.65
    elif f["bypass_verification"]:
        score = 0.4
    return score, flags

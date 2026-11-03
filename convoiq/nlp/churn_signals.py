"""Conversation-level churn signals (competitor, cancel/port-out intent, price complaints)."""
from __future__ import annotations

import re

from convoiq.contracts import ChurnSignals, Speaker, Turn
from convoiq.nlp.entities import find_competitors
from convoiq.nlp.text import customer_turns

CANCEL = re.compile(r"\b(cancel(?:l?ing)? (?:my |the )?(?:service|line|account|plan|contract|subscription)?|"
                    r"close (?:my )?account|terminate|leave you|leaving|switch(?:ing)? (?:carriers?|providers?|networks?|away)|"
                    r"take my (?:number|business)|taking my (?:number|business)|why should i stay|reason to stay|convince me to stay|"
                    r"(?:thinking|thinking of|considering) (?:of |about )?(?:leaving|switching|cancel\w*))\b", re.I)
PORT = re.compile(r"\b(port(?:ing)? (?:my number )?out|transfer (?:my )?(?:number|pin)|pac code|unlock code|"
                  r"port(?:ing)? (?:it|my number) to)\b", re.I)
PRICE = re.compile(r"\b(too expensive|expensive|overpriced|cheaper|cheap(?:er)? (?:plan|price|deal)|price (?:hike|increase)|"
                   r"can'?t afford|better (?:deal|offer|price)|why should i stay|per month for)\b", re.I)
# customer is merely *comparing*, not leaving
BENIGN_COMPARE = re.compile(r"\b(my (?:friend|brother|sister|colleague|wife|husband)|unlike|happy with you|love your)\b", re.I)


MOVE_TO = re.compile(r"\b(?:switch(?:ing)?|mov(?:e|ing)|go(?:ing)?|port(?:ing)?|leav(?:e|ing)|jump(?:ing)?)\b[^.!?]{0,40}\bto\b[^.!?]{0,20}$", re.I)


def churn_signals(turns: list[Turn], tenure_months: int | None = None) -> ChurnSignals:
    cust = customer_turns(turns)
    comps: set[str] = set()
    cancel = port = price = False
    ev: list[int] = []
    score = 0.0
    for t in cust:
        txt = t.text
        c_m = find_competitors(txt)
        is_cancel = bool(CANCEL.search(txt)) or bool(c_m and re.search(r"\b(switch|mov|go|port|leav|jump)\w*\b", txt, re.I)
                                                      and not BENIGN_COMPARE.search(txt))
        is_port = bool(PORT.search(txt))
        is_price = bool(PRICE.search(txt))
        benign = bool(BENIGN_COMPARE.search(txt)) and not (is_cancel or is_port)
        if c_m and not benign:
            comps |= c_m
        if is_cancel or is_port or (is_price and c_m):
            ev.append(t.i)
        cancel |= is_cancel and not benign
        port |= is_port
        price |= is_price
        if benign and c_m:
            continue
    if comps and (cancel or port or price):
        score += 0.35
    score += 0.3 * port + 0.25 * cancel + 0.1 * price
    if comps - set() and not (cancel or port):
        score += 0.05
    if tenure_months is not None and tenure_months < 12:
        score += 0.05
    return ChurnSignals(competitor_mentions=sorted(comps), cancel_intent=cancel, port_out_request=port,
                        price_complaint=price, tenure_months=tenure_months, score=round(min(1.0, score), 3),
                        evidence_turns=sorted(set(ev)))

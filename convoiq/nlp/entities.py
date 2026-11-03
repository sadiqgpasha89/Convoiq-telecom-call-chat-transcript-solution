"""Telecom entity extraction (rules + gazetteers; deterministic and auditable)."""
from __future__ import annotations

import re

from convoiq.contracts import Turn
from convoiq.taxonomy import COMPETITORS, COMPETITORS_AMBIGUOUS

PLAN_NAMES = ["basic plan", "premium plan", "unlimited plus", "family share", "iot basic", "unlimited", "prepaid", "postpaid"]
TECH_TERMS = ["volte", "vowifi", "wifi calling", "wi-fi calling", "apn", "esim", "e-sim", "sim card", "puk", "imsi", "enodeb",
              "5g", "4g", "3g", "lte", "roaming", "mms", "sms", "voicemail", "hotspot", "tethering", "airplane mode",
              "data saver", "vpn", "network mode", "router", "fiber", "ont", "port out", "porting", "pac code"]
DEVICES = ["iphone", "pixel", "galaxy", "oneplus", "ipad", "smartphone x", "smartphone pro max", "tablet ultra", "smartphone lite"]
MONEY = re.compile(r"\$\s?(\d{1,5}(?:,\d{3})*(?:\.\d{1,2})?)")
DATA_AMT = re.compile(r"\b(\d+(?:\.\d+)?)\s?(gb|mb|gigabytes?)\b", re.I)
TOWER = re.compile(r"\b(?:TWR|TOWER|CELL)[-_ ]?[A-Z]{2,4}[-_ ]?\d{2,4}\b", re.I)
TOKEN = re.compile(r"<PII_([A-Z]+)_[0-9A-F]+>")
COMP_RX = re.compile(r"\b(" + "|".join(re.escape(c) for c in sorted(COMPETITORS, key=len, reverse=True)) + r")\b", re.I)


_CHURN_CTX = re.compile(r"\b(switch\w*|mov(?:e|ing)|port\w*|cancel\w*|leav\w*|cheaper|offer\w*|deal|plan|customer|join\w*|jump\w*|stay)\b", re.I)
_AMB_AFTER_BAD = re.compile(r"^\s+(it|that|this|the|my|your|up|speed|signal|performance|from|and|a|our|its|mode|wifi|wi-fi|data)\b", re.I)
_AMB_BEFORE = re.compile(r"\b(to|from|with|at|for|join|joining|on|using)\s+$", re.I)
_AMB_AFTER_OK = re.compile(r"^\s+(mobile|wireless|network|prepaid|offer|plan|deal)\b", re.I)
_AMB_AFTER_BRAND = re.compile(r"^\s+(is|are|has|have|offers?|offering|gives?|giving|customers?)\b", re.I)


def find_competitors(text: str) -> set[str]:
    """Competitor mentions. Ambiguous English words (boost, three, visible, ...) only count in clear brand context
    (capitalised + brand verb, or preceded by a switching preposition in a churn-flavoured sentence)."""
    found: set[str] = set()
    for m in COMP_RX.finditer(text):
        name = m.group(1).lower()
        if name not in COMPETITORS_AMBIGUOUS:
            found.add(name)
            continue
        before, after = text[: m.start()], text[m.end():]
        if _AMB_AFTER_OK.match(after) or (m.group(1)[:1].isupper() and _AMB_AFTER_BRAND.match(after)):
            found.add(name)
        elif _AMB_BEFORE.search(before) and not _AMB_AFTER_BAD.match(after) and _CHURN_CTX.search(text):
            found.add(name)
    return found


def extract_entities(turns: list[Turn]) -> dict[str, list[str]]:
    text = " \n ".join(t.text for t in turns if t.text)
    low = text.lower()
    ents: dict[str, list[str]] = {
        "plans": sorted({p for p in PLAN_NAMES if p in low}),
        "tech_terms": sorted({t for t in TECH_TERMS if re.search(r"\b" + re.escape(t) + r"\b", low)}),
        "devices": sorted({d for d in DEVICES if d in low}),
        "amounts_usd": sorted({m.group(1) for m in MONEY.finditer(text)}),
        "data_amounts": sorted({f"{m.group(1)}{m.group(2).lower()[:2]}" for m in DATA_AMT.finditer(text)}),
        "towers": sorted({m.group(0).upper().replace(" ", "-") for m in TOWER.finditer(text)}),
        "competitors": sorted(find_competitors(text)),
        "pii_types_present": sorted({m.group(1) for m in TOKEN.finditer(text)}),
    }
    return {k: v for k, v in ents.items() if v}

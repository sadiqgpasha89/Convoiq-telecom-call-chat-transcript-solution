"""Telecom-tuned, explainable sentiment / frustration / sarcasm scoring (no external downloads).

Lexicon-based with negation, intensifiers, caps/exclamation cues and telecom-specific phrases.
It produces a *per-customer-turn valence arc* (the unit that matters for CSAT prediction), a
frustration index (repetition, escalation demands, legal/regulatory threats) and a sarcasm score
(positive surface form against a negative situation).  Validation is in ``convoiq.evals`` (arc
direction accuracy on labelled synthetic data; correlation with the tau2 "Hard" persona).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from convoiq.contracts import SentimentTrace, Turn
from convoiq.nlp.text import customer_turns

POS = {"thanks": 0.5, "thank": 0.5, "great": 0.6, "perfect": 0.7, "awesome": 0.8, "excellent": 0.8, "good": 0.4,
       "happy": 0.7, "appreciate": 0.6, "helpful": 0.6, "wonderful": 0.7, "love": 0.6, "resolved": 0.5, "works": 0.5,
       "working": 0.3, "fixed": 0.6, "fine": 0.2, "okay": 0.1, "ok": 0.1, "sure": 0.1, "glad": 0.6, "amazing": 0.7,
       "fair": 0.3, "nice": 0.5, "super": 0.5, "cheers": 0.4, "brilliant": 0.7, "relief": 0.4}
NEG = {"terrible": -0.8, "awful": -0.8, "horrible": -0.8, "worst": -0.9, "ridiculous": -0.8, "useless": -0.8,
       "angry": -0.8, "furious": -0.9, "frustrated": -0.7, "frustrating": -0.7, "annoyed": -0.6, "annoying": -0.6,
       "unacceptable": -0.8, "scam": -0.9, "robbery": -0.9, "rip": -0.5, "overcharged": -0.7, "charged": -0.3,
       "slow": -0.4, "dead": -0.5, "broken": -0.6, "failed": -0.6, "failing": -0.6, "problem": -0.3, "issue": -0.2,
       "disappointed": -0.6, "upset": -0.6, "hate": -0.8, "bad": -0.5, "poor": -0.5, "never": -0.2, "wrong": -0.4,
       "nothing": -0.2, "still": -0.2, "again": -0.3, "dropped": -0.5, "drops": -0.5, "dropping": -0.5, "cancel": -0.4,
       "stuck": -0.5, "waiting": -0.3, "waste": -0.6, "stolen": -0.4, "hours": -0.2, "pathetic": -0.9, "joke": -0.6,
       "unhappy": -0.6, "mess": -0.6, "fraud": -0.5, "complaint": -0.5, "refuse": -0.5, "sucks": -0.8, "lousy": -0.7,
       "disgusted": -0.8, "fed": -0.3, "nonsense": -0.7, "incompetent": -0.9, "outrageous": -0.8, "damn": -0.5}
# multi-word expressions
PHRASES = {"fed up": -0.8, "sick of": -0.8, "had enough": -0.8, "no signal": -0.5, "no service": -0.5,
           "not working": -0.6, "doesn't work": -0.6, "does not work": -0.6, "won't work": -0.6, "can't connect": -0.5,
           "cannot connect": -0.5, "third time": -0.7, "second time": -0.4, "waste of": -0.7, "taking my business": -0.8,
           "thank you so much": 0.8, "that works": 0.5, "all set": 0.5, "that's all i needed": 0.5, "works now": 0.7,
           "it works": 0.6, "problem solved": 0.8, "much better": 0.6}
NEGATORS = {"not", "no", "never", "n't", "don't", "doesn't", "didn't", "won't", "can't", "cannot", "isn't", "wasn't"}
INTENSIFY = {"very": 1.3, "really": 1.3, "so": 1.2, "extremely": 1.5, "absolutely": 1.5, "totally": 1.3, "completely": 1.4,
             "truly": 1.3, "utterly": 1.5}
ESCALATE = re.compile(r"\b(supervisor|manager|escalate|higher up|someone else|speak to (?:a|your) (?:human|person|manager)|"
                      r"real person|complain(?:t)? (?:to|with)|ombudsman|regulator|fcc|ofcom|trai|lawyer|legal action|sue|"
                      r"report you|social media|review online)\b", re.I)
REPEAT = re.compile(r"\b(again|still|third time|second time|already (?:told|explained|called)|every time|keeps? (?:happening|"
                    r"dropping|failing)|over and over|as i (?:said|mentioned)|how many times|for the (?:\w+) time)\b", re.I)
POS_SURFACE = {"great", "wonderful", "amazing", "fantastic", "love", "perfect", "excellent", "brilliant", "awesome", "thanks"}
_WORD = re.compile(r"[a-z']+")


def _score_text(text: str) -> tuple[float, int]:
    """Valence in [-1,1] for a single utterance and the count of lexicon hits."""
    low = text.lower()
    total, hits = 0.0, 0
    for ph, v in PHRASES.items():
        if ph in low:
            total += v
            hits += 1
            low = low.replace(ph, " ")
    words = _WORD.findall(low)
    for i, w in enumerate(words):
        v = POS.get(w, NEG.get(w))
        if v is None:
            continue
        window = words[max(0, i - 3): i]
        if any(x in NEGATORS or x.endswith("n't") for x in window):
            v = -0.6 * v
        for x in window:
            if x in INTENSIFY:
                v *= INTENSIFY[x]
        total += v
        hits += 1
    excl = text.count("!")
    letters = [c for c in text if c.isalpha()]
    caps = (sum(c.isupper() for c in letters) / len(letters)) if len(letters) > 8 else 0.0
    if hits and total < 0:
        total *= 1 + min(0.3, 0.05 * excl) + (0.3 if caps > 0.5 else 0)
    if hits == 0:
        return 0.0, 0
    # squash to [-1,1], dampen by sqrt(hits) so one word is not saturated
    score = total / (1.0 + 0.5 * (hits - 1) + abs(total) * 0.35)
    return max(-1.0, min(1.0, score)), hits


def sarcasm_score(text: str, situation_negative: bool) -> float:
    """Positive surface words used in a negative situation ("Oh wonderful, only 3 hours without signal")."""
    low = text.lower()
    pos = any(w in POS_SURFACE for w in _WORD.findall(low))
    neg_ctx = any(NEG.get(w, 0) < -0.3 for w in _WORD.findall(low)) or any(p in low for p in PHRASES if PHRASES[p] < -0.4)
    lead = bool(re.match(r"^\s*(oh|wow|yeah|sure|great|wonderful|brilliant)\b", low))
    s = 0.0
    if pos and neg_ctx:
        s += 0.55
    if pos and situation_negative:
        s += 0.25
    if lead and (neg_ctx or situation_negative):
        s += 0.15
    if re.search(r"\b(really love|truly|so much|just what i needed|thanks for nothing)\b", low) and (neg_ctx or situation_negative):
        s += 0.15
    return min(1.0, s)


def analyze_sentiment(turns: list[Turn]) -> SentimentTrace:
    cust = customer_turns(turns)
    if not cust:
        return SentimentTrace()
    scores, ids, sarc, esc, rep = [], [], [], 0, 0
    # first pass to establish situation negativity
    raw = [_score_text(t.text)[0] for t in cust]
    situation_negative = (sum(raw) / len(raw)) < -0.1 or any(NEG.get(w, 0) <= -0.5 for t in cust for w in _WORD.findall(t.text.lower()))
    for t, s in zip(cust, raw):
        sc = sarcasm_score(t.text, situation_negative)
        if sc >= 0.55 and s > 0:
            s = -abs(s) * 0.8 - 0.2          # sarcastic praise is negative valence
        scores.append(round(s, 4))
        ids.append(t.i)
        sarc.append(sc)
        esc += len(ESCALATE.findall(t.text))
        rep += len(REPEAT.findall(t.text))
    n = len(scores)
    k = max(1, n // 3)
    first, last = sum(scores[:k]) / k, sum(scores[-k:]) / k
    delta = last - first
    trend = "improving" if delta > 0.15 else "worsening" if delta < -0.15 else "flat"
    neg_share = sum(s < -0.25 for s in scores) / n
    frustration = min(1.0, 0.45 * neg_share + 0.12 * min(esc, 3) + 0.08 * min(rep, 4) + 0.3 * max(0.0, -min(scores)) )
    return SentimentTrace(per_turn=scores, turn_ids=ids, mean=round(sum(scores) / n, 4), minimum=round(min(scores), 4),
                          delta=round(delta, 4), trend=trend, frustration_index=round(frustration, 4),
                          sarcasm_score=round(max(sarc), 4), escalation_demands=esc)

"""Privacy: recall / leakage / over-redaction on labelled PII, ASR-noise robustness, decoy preservation."""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from convoiq.evals.context import EvalContext
from convoiq.pipeline import corpus_to_raw
from convoiq.privacy.redactor import second_pass
from convoiq.privacy.tokenizer import TOKEN_RE

DIGIT_TYPES = {"PHONE", "CARD", "SSN", "PIN", "CVV"}


def _leaked(value: str, typ: str, text: str) -> bool:
    if value in text:
        return True
    if typ in DIGIT_TYPES:
        d = re.sub(r"\D", "", value)
        return len(d) >= 4 and d in re.sub(r"\D", "", text)
    return False


def _score(ctx: EvalContext, convs: list[dict]) -> dict:
    per_type: dict[str, list[int]] = defaultdict(lambda: [0, 0])      # [caught, total]
    convs_leaking = decoys_ok = decoys_total = extra_tokens = gold_n = resid = resid_after = 0
    for c in convs:
        raw = corpus_to_raw(c)
        red = ctx.pipeline.redact(raw)
        texts = {t.i: t.text for t in red.turns}
        leak = False
        for g in c["labels"]["pii"]:
            ok = not _leaked(g["value"], g["type"], texts.get(g["turn"], ""))
            per_type[g["type"]][0] += ok
            per_type[g["type"]][1] += 1
            leak |= not ok
        convs_leaking += leak
        alltext = " ".join(texts.values())
        for d in c["labels"].get("decoys", []):
            decoys_total += 1
            decoys_ok += d.split()[-1] in alltext        # decoy label is 'Order 4400..': the identifier itself must survive
        n_tok = len(TOKEN_RE.findall(alltext))
        gold_n += len(c["labels"]["pii"])
        extra_tokens += max(0, n_tok - len(c["labels"]["pii"]))
        resid += red.attestation.residual_findings > 0
        if red.attestation.residual_findings:
            resid_after += second_pass(ctx.pipeline.redactor, red).attestation.residual_findings > 0
    tot = sum(v[1] for v in per_type.values())
    out = {"n_conversations": len(convs), "recall_overall": sum(v[0] for v in per_type.values()) / max(tot, 1),
           "conversation_leakage_rate": convs_leaking / len(convs), "decoy_preservation": decoys_ok / max(decoys_total, 1),
           "extra_tokens_per_conversation": extra_tokens / len(convs), "residual_after_first_pass_rate": resid / len(convs),
           "residual_after_second_pass_rate": resid_after / len(convs)}
    out.update({f"recall_{t}": v[0] / v[1] for t, v in sorted(per_type.items())})
    return out


def run(ctx: EvalContext) -> dict:
    res = {}
    clean = _score(ctx, ctx.synth(seeded=False))
    res.update({k: v for k, v in clean.items()})
    noisy = _score(ctx, ctx.synth(n=min(ctx.n_syn, 300), seed=ctx.seed + 1, noise=0.15, seeded=False))
    res["noisy_recall_overall"] = noisy["recall_overall"]
    res["noisy_conversation_leakage_rate"] = noisy["conversation_leakage_rate"]
    # real(ish) text: tau2 conversations contain few identifiers; measure the *rate* of redaction and show what was caught
    types: Counter = Counter()
    per_conv = []
    examples = []
    for c in ctx.tau2_test[:300]:
        red = ctx.pipeline.redact(corpus_to_raw(c))
        per_conv.append(sum(red.attestation.counts.values()))
        types.update(red.attestation.counts)
        if red.attestation.counts and len(examples) < 6:
            for t in red.turns:
                m = TOKEN_RE.search(t.text)
                if m:
                    examples.append({"turn_role": t.role.value, "snippet": t.text[max(0, m.start() - 50): m.end() + 30]})
                    break
    res["tau2_redactions_per_conversation"] = sum(per_conv) / max(len(per_conv), 1)
    res["tau2_redaction_types"] = dict(types)
    res["_examples_tau2_redactions_for_manual_review"] = examples
    return res

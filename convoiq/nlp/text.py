"""Shared text utilities."""
from __future__ import annotations

import re
from typing import Iterable

from convoiq.contracts import Speaker, Turn

_WS = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"<PII_[A-Z0-9]+_[0-9A-F]+>")


def norm(text: str) -> str:
    """Lowercase, collapse whitespace, replace PII tokens with a generic marker."""
    text = _TOKEN_RE.sub(lambda m: " " + m.group(0).split("_")[1].lower() + "tok ", text)
    return _WS.sub(" ", text.lower()).strip()


def customer_turns(turns: Iterable[Turn]) -> list[Turn]:
    return [t for t in turns if t.role == Speaker.CUSTOMER and t.text]


def agent_turns(turns: Iterable[Turn]) -> list[Turn]:
    return [t for t in turns if t.role in (Speaker.AGENT, Speaker.BOT) and t.text]


def tool_tokens(turns: Iterable[Turn], with_results: bool = True) -> str:
    """Tool-call names (and abbreviated results) as pseudo-words: strong signal for root cause."""
    toks: list[str] = []
    for t in turns:
        for tc in t.tool_calls:
            toks.append(f"tc_{tc.name}")
            for k, v in (tc.arguments or {}).items():
                if isinstance(v, (str, bool, int, float)) and len(str(v)) < 24:
                    toks.append(f"tcarg_{tc.name}_{k}_{str(v).lower().replace(' ', '_')}")
        if with_results and t.role == Speaker.TOOL and t.text:
            toks.append("toolres " + norm(t.text)[:200])
    return " ".join(toks)


def conversation_text(turns: list[Turn], include_tools: bool = True, max_turns: int | None = None,
                      roles: set[Speaker] | None = None) -> str:
    sel = turns[:max_turns] if max_turns else turns
    parts = []
    for t in sel:
        if roles and t.role not in roles:
            continue
        if t.text and t.role != Speaker.TOOL:
            prefix = "cust" if t.role == Speaker.CUSTOMER else "agnt"
            parts.append(f"{prefix}: {norm(t.text)}")
    txt = " \n ".join(parts)
    if include_tools:
        txt += " \n " + tool_tokens(sel)
    return txt


def corpus_turns_to_models(turns: list[dict]) -> list[Turn]:
    """Convert corpus-dict turns (tau2/synthetic JSONL) to Turn models."""
    out = []
    for t in turns:
        role = Speaker(t["role"]) if t["role"] in {s.value for s in Speaker} else Speaker.SYSTEM
        out.append(Turn(i=t["i"], role=role, text=t.get("text", ""),
                        tool_calls=[{"name": c["name"], "arguments": c.get("arguments", {}),
                                     "requestor": c.get("requestor", "assistant")} for c in t.get("tool_calls", [])],
                        requestor=t.get("requestor"), is_error=bool(t.get("is_error"))))
    return out

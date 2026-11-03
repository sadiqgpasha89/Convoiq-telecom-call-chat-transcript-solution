"""Tiny async DAG executor for the agent mesh (explicit, inspectable alternative to a framework).

Why not LangGraph/AutoGen here?  The orchestration is a *fixed*, auditable DAG with typed state - the
spec's requirements (deterministic fallbacks, per-node timeouts, optional nodes, full trace for the
decision record, cheap unit tests) are met with ~100 lines and no extra runtime dependency.  Nodes are
plain async functions, so porting to LangGraph later is mechanical (see docs/adr/0004).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from convoiq.contracts import TraceStep
from convoiq.logging import get_logger
from convoiq.platform.metrics import REGISTRY

log = get_logger("convoiq.graph")
_node_dur = REGISTRY.histogram("convoiq_agent_node_seconds", "agent node latency", ["node", "status"])

NodeFn = Callable[[dict], Awaitable[Any]]


@dataclass
class Node:
    name: str
    fn: NodeFn
    deps: tuple[str, ...] = ()
    timeout_s: float = 5.0
    optional: bool = True                 # failure degrades the result instead of failing the run
    skip_if: Callable[[dict], bool] | None = None


@dataclass
class GraphResult:
    state: dict
    trace: list[TraceStep] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)


class Graph:
    def __init__(self, nodes: list[Node]):
        self.nodes = {n.name: n for n in nodes}
        for n in nodes:
            for d in n.deps:
                assert d in self.nodes, f"unknown dep {d} for {n.name}"

    async def run(self, state: dict) -> GraphResult:
        done: dict[str, bool] = {}
        res = GraphResult(state)
        remaining = dict(self.nodes)
        while remaining:
            ready = [n for n in remaining.values() if all(d in done for d in n.deps)]
            if not ready:
                raise RuntimeError("cycle or unsatisfied dependency in agent graph")
            outs = await asyncio.gather(*[self._run_node(n, state) for n in ready])
            for n, (step, ok) in zip(ready, outs):
                res.trace.append(step)
                done[n.name] = ok
                remaining.pop(n.name)
                if not ok:
                    res.degraded.append(f"{n.name}: {step.detail}")
                    if not n.optional:
                        raise RuntimeError(f"required node '{n.name}' failed: {step.detail}")
        return res

    async def _run_node(self, n: Node, state: dict) -> tuple[TraceStep, bool]:
        if n.skip_if and n.skip_if(state):
            return TraceStep(node=n.name, status="skipped"), True
        t0 = time.perf_counter()
        try:
            out = await asyncio.wait_for(n.fn(state), n.timeout_s)
            if out is not None:
                state[n.name] = out
            status, detail, ok = "ok", "", True
        except asyncio.TimeoutError:
            status, detail, ok = "timeout", f"exceeded {n.timeout_s}s", False
        except Exception as e:
            log.warning("node failed", node=n.name, error=f"{type(e).__name__}: {e}")
            status, detail, ok = "error", f"{type(e).__name__}: {e}"[:200], False
        dur = time.perf_counter() - t0
        _node_dur.observe(dur, node=n.name, status=status)
        return TraceStep(node=n.name, status=status, duration_ms=round(dur * 1000, 2), detail=detail), ok

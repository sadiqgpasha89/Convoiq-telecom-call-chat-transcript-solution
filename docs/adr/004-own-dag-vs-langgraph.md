# ADR-004: Small in-house async DAG executor instead of LangGraph/ADK
**Status:** accepted
**Why:** Needed per-node timeouts, optional-node degradation, deterministic ordering, zero-LLM operation and trivially testable nodes; ~150 lines. The graph is a dependency DAG, not a cyclic planner.
**Consequences:** No built-in checkpointing/visualisation. Agent nodes use plain async functions with typed inputs, so porting to ADK/LangGraph is mechanical if cyclic reasoning is later required.

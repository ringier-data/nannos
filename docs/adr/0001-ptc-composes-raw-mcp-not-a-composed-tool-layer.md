---
status: accepted
---

# PTC composes raw host primitives in code; no composed-MCP-tool layer

For Embedded Nannos acting on a host application's behalf, we expose the host's
API as a **raw, endpoint-shaped MCP server** and let **Programmatic Tool Calling
(PTC)** compose those primitives *in sandbox code* at runtime — the same way the
host's own front-end composes SDK calls. We deliberately do **not** build a
semantic "ontology-shaped" MCP tool layer that pre-composes API calls into
higher-level tools.

## Why

A 1:1 endpoint→tool projection is intractable for direct tool-calling (the model
drowns in dozens of low-level CRUD tools), which is what first pushed us toward a
composed layer. But PTC already exists in `agent-common`
(`CODE_INTERPRETER_PTC`, `wrap_tool_for_ptc`) and dissolves the problem
differently: composition becomes ordinary code (loops, conditionals, filtering),
and tractability is handled by **progressive disclosure** — the agent
greps/reads only the API types it needs from sandbox files. Pre-composing tools
would freeze in config what code does more flexibly, and would require an
ongoing authoring/regeneration pipeline per host as APIs drift.

## Considered options

- **Composed/ontology-shaped MCP tools** (static codegen or a runtime adapter
  agent): inspectable and deterministic, but brittle to API drift, frozen to
  anticipated compositions, and high per-host authoring cost.
- **PTC over raw MCP (chosen)**: flexible, drift-tolerant, near-zero per-host
  composition authoring; composition emerges in code.

## Consequences

- The semantic knowledge that *would* have lived in composed tools instead lives
  in an **ontology skill** (see ADR-0003 / CONTEXT.md) used as grounding.
- Determinism/inspectability is traded for runtime flexibility. This is
  acceptable only because the **per-call PTC risk scorer + HITL guard** gates
  every write made from inside the generated code — safety does not depend on
  pre-composition.
- An endpoint-shaped generated MCP server is acceptable precisely *because* of
  PTC + progressive disclosure; it would be an anti-pattern under direct
  tool-calling.

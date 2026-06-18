# Nannos — Model Provisioning Context

How the Nannos platform provisions, selects, invokes, and accounts for LLM models across its agent services (orchestrator, agent-runner, agent-creator, console-backend).

## Language

**Model Gateway**:
The unified interface through which all LLM calls are routed, replacing per-provider clients. Implemented as a LiteLLM Proxy.
_Avoid_: "LiteLLM" alone (the library is the implementation, not the role), "model router"

**LiteLLM Proxy**:
The standalone pod running LiteLLM in server mode, holding provider routing/credentials and exposing an OpenAI-compatible inference API plus a management API for runtime model CRUD.
_Avoid_: "LiteLLM SDK" (the rejected in-process alternative)

**Model Alias**:
The stable, provider-agnostic name an app uses to request a model (e.g. `claude-sonnet-4.6`). Resolved by the Gateway to a concrete provider + provider model id. The app never names a provider model id directly.
_Avoid_: "model name" (ambiguous — see Flagged ambiguities), "model id"

**Capability**:
What a model can do — input modalities, extended-thinking support and levels, context window. Drives the model-picker UI and construction behavior. Stored in the Gateway's `model_info`.

**model_info**:
The per-model metadata block on the LiteLLM Proxy holding Capability plus the Gateway's own cost data. The source of truth for routing + capability. Written and edited exclusively through console-backend (never hand-edited on the proxy).

**Rate Card**:
The Nannos-owned billing record, keyed on `(provider, model_name, billing_unit)`, that turns a usage breakdown into money. Lives in console-backend's DB. Richer than the Gateway's cost map: it supports **agent-specific pricing** (per `sub_agent_config_version_id`), **time-versioned rates** (`effective_from`), and **billing-unit fallback**. Cannot be replaced by LiteLLM cost data.
_Avoid_: "pricing", "cost map" (the latter is LiteLLM's built-in data, not the Rate Card)

**Usage Event**:
A single LLM call's measured consumption — a `billing_unit_breakdown` (base/cache/reasoning input & output tokens) plus the model and Cost Attribution. The input to Rate Card costing.

**Cost Attribution**:
The mapping of a Usage Event to *who* to bill — user / sub-agent / conversation / scheduled job. Today carried in LangGraph config tags. The Gateway can also receive it per-request as `spend_logs_metadata` (arbitrary keys), so attribution is *not* inherently invisible to the Gateway.

## Relationships

- An app requests a **Model Alias**; the **Model Gateway** resolves it to a provider + provider model id.
- A **Model Alias** has one **Capability** set and one or more **Rate Card** entries.
- A usage event combines a **Rate Card** (how much) with **Cost Attribution** (who) to produce a billed cost.

## Flagged ambiguities

- "model name" was used for both the user-facing **Model Alias** and the provider's concrete model id — resolved: **Model Alias** is the app-facing name; "provider model id" is the concrete target. Note: the **Rate Card** key field is literally named `model_name` in the DB and stores the **Model Alias** value.

## Decisions

- **Q1 (locked):** The Model Gateway is deployed as a standalone **LiteLLM Proxy pod**, not the in-process SDK. See [ADR-0001](docs/adr/0001-litellm-proxy-pod-as-model-gateway.md).
- **Q5 (locked):** Proxy `model_info` is the source of truth for routing + capability; console-backend's **Rate Card** owns billing. console-backend can create *and* edit `model_info` via the management API — it is the sole write path into the proxy registry.
- **Q6a (locked):** Two-store consistency via an app-owned Model Registration lifecycle (`draft → validated → active`). A **Rate Card must exist before a model goes `active`** (kills the cost-leak failure mode). On register: Rate Card row → proxy `model_info` → `validated`; promotion to `active` is explicit, post test-call. Rate Card cost is **pre-filled (seed)** from the Gateway's `/model/info` provider base cost; admin confirms/overrides.
- **Q4 (locked):** Usage Event capture moves to a proxy-side LiteLLM `CustomLogger` (4-B); attribution travels as `spend_logs_metadata` via a ContextVar→httpx-header hook; pricing stays in `RateCardService.calculate_cost`. See [ADR-0002](docs/adr/0002-usage-capture-at-the-gateway.md).
- **Q4-ditch (resolved):** Rate Card is **not** retired. Blocker is the pricing model (per-sub-agent pricing + rate versioning), not attribution. Off-ramp is a product decision only. See ADR-0002 Considered Options.
- **Q2 (locked):** Adopt LiteLLM unified `reasoning_effort`; app keeps the `thinking_level → reasoning_effort` mapping; delete the explicit `get_thinking_budget` token-budget map. See [ADR-0003](docs/adr/0003-unified-reasoning-effort.md).
- **Q3 (locked; gate resolved 2026-06-18):** Proxy `stream_timeout`+`timeout` (3-A) as outer bounds, **plus a mandatory client-side inter-chunk watchdog (3-C)** — the spike proved the proxy silently ignores `stream_timeout` on Bedrock streaming (#23375), so proxy-only is insufficient. See [ADR-0004](docs/adr/0004-timeout-strategy.md).
- **Q6-auth (locked):** OSS LiteLLM has no route-scoped keys (RBAC is Enterprise), so console-backend holds the **master key** server-side; scoping is enforced in console-backend (`require_admin` + audit) plus network isolation of the management routes + key rotation. See [ADR-0005](docs/adr/0005-gateway-management-auth.md).
- **Q6-validation (locked):** `validated → active` promotion is a **manual admin action** after a successful test call (not an automated check).

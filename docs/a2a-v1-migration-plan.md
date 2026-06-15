# A2A Python SDK Migration Plan: `a2a-sdk` v0.3 → v1.1.0

> Status: Draft for review · Author: Engineering · **Target SDK: `a2a-sdk==1.1.0`** (latest; verified directly).

> **✅ Phase 0 verified against the real wheel — `a2a-sdk==1.1.0` (the chosen target), in a throwaway venv.** Confirmed: type system is genuinely protobuf (`a2a_pb2.*`, not Pydantic); `TextPart`/`FilePart`/`DataPart`/`FileWith*` removed; `Part` fields are exactly `text, raw, url, data, metadata, filename, media_type`; `Role.ROLE_USER/ROLE_AGENT` and `TaskState.TASK_STATE_*`; `AgentCard` has no top-level `url` (uses `supported_interfaces`/`AgentInterface`, which also carries a `tenant` field); `AgentCapabilities` has `extended_agent_card` + `extensions`; `a2a.server.apps` is gone; `a2a.helpers` exists with all expected functions; `DefaultRequestHandler` requires `agent_card` (3rd positional); `create_jsonrpc_routes`/`create_rest_routes` exist with `enable_v0_3_compat`; server-side `TaskStore`/`DatabaseTaskStore`/push/`agent_execution` import paths are **unchanged**. **Corrected vs the docs:** (1) `ClientFactory` is **NOT removed** — `ClientFactory(config).create(card, interceptors=…)` still works, so our client wiring largely survives; (2) `ClientConfig.supported_transports` and the `TransportProtocol` enum are **removed** (this, not `ClientFactory`, is the real client break); (3) `ClientConfig.push_notification_config` is **single** (not a list); `send_message()` returns `AsyncIterator[StreamResponse]`. **Version-sensitive note:** `add_a2a_routes_to_fastapi(app, *, agent_card_routes, jsonrpc_routes, rest_routes)` **exists in 1.1.0** (it was absent in 1.0.1 — added after) — so the convenient FastAPI mount **is** available on our target. See §2.4/§2.5.
> Scope: `packages/ringier-a2a-sdk`, `agent-common`, `console-backend`, `orchestrator-agent`, `agent-creator`, `agent-runner`, `voice-agent`

---

## 1. Executive Summary of Changes

A2A 1.0 is the protocol's "production readiness" milestone. For the Python SDK this is **not a routine version bump** — it is a foundational rewrite that touches almost every line of A2A-facing code we have. Four shifts dominate:

1. **Type system: Pydantic → Protobuf.** All wire types (`Message`, `Task`, `Part`, `Artifact`, `AgentCard`, …) are now Protobuf-generated classes serialized with **ProtoJSON**, not Pydantic models. This breaks every place we use `model_dump()`, the `Part(root=…)` `RootModel` pattern, `part.root.kind`, `isinstance(inner, TextPart)`, and arbitrary attribute assignment. This is the single largest source of churn in our codebase.

2. **Enums: snake_case → SCREAMING_SNAKE_CASE.** `TaskState.completed` → `TaskState.TASK_STATE_COMPLETED`, `Role.user` → `Role.ROLE_USER`, etc. We compare against these enums in state machines across `agent-common`, `orchestrator-agent`, and `ringier-a2a-sdk`.

3. **`Part` is unified.** `TextPart` / `FilePart` / `DataPart` and the `Part(root=…)` wrapper are **removed**. A `Part` now carries content directly via the populated field (`text`, `raw`, `url`, `data`), with the file `kind` discriminator gone and `FilePart`/`DataPart` flattened. Our part-conversion utility and every message builder must be rewritten.

4. **`AgentCard` restructured + server/client API replaced.** The top-level `url` is gone (→ `supported_interfaces=[AgentInterface(...)]`); `supports_authenticated_extended_card` → `AgentCapabilities.extended_agent_card`; `examples` moved to `AgentSkill`. The wrapper apps (`A2AFastAPIApplication`, `A2AStarletteApplication`) and `ClientFactory` are **removed** in favor of route factories (`create_*_routes`) and `await create_client(...)`.

**Rollout strategy — big-bang.** We deliberately keep everything in one monorepo so the whole fleet ships together. We therefore migrate and deploy all packages in a **single coordinated release**, with **no `enable_v0_3_compat` bridge and no dual `protocol_version` interfaces**. Every agent and client advertises and speaks v1.0 only. This removes an entire class of compatibility shims and read-time translation code from the plan.

**Data persistence — no migration needed.** `orchestrator-agent`'s `DatabaseTaskStore` is **greenfield** (never deployed anywhere, uncommitted on this branch), so there is no stored data to migrate — we build it directly against v1.0. The only pre-existing persisted A2A data is the **`console-backend` message store** (chat history), and we've decided that history is **disposable: it gets cleared on cutover** (§2.8). Net result: zero data-backfill work in this migration.

**Effort estimate:** Large but bounded. `ringier-a2a-sdk` is the foundation every package depends on and must migrate first. Sequencing is 4 phases (see §5); the protobuf type-system conversion is the highest-risk item, followed by the console-backend message-store question.

---

> **✅ Phase 1 done** — `ringier-a2a-sdk` migrated to 1.1.0, 248 tests green. **🔬 Phase 2 (agent-common) verified against the real 1.1.0 wheel** — several corrections beyond Phase 0:
> - **`uv sync` gotcha:** bumping a pin + `uv lock` does NOT update the venv; you must `uv sync`, or you introspect/test against the *stale* 0.3.x install (its Pydantic `RootModel` `Part` looks deceptively like a valid result). Verify `importlib.metadata.version("a2a-sdk")` after syncing.
> - **Client is lower-level than the docs implied:** `Client.send_message(request: SendMessageRequest) -> AsyncIterator[StreamResponse]` (not a bare `Message`, not `(Task, Update)` tuples — those were the 0.3 shape). `StreamResponse` has a **oneof `payload` = {task, message, status_update, artifact_update}** → branch with `chunk.WhichOneof("payload")`. So the streaming loop is a real rewrite, not a field-rename.
> - **`TaskIdParams` / `ClientEvent` are gone.** Cancel via `client.cancel_task(CancelTaskRequest(id=task_id))`. `SendMessageRequest`/`StreamResponse`/`CancelTaskRequest` are re-exported from `a2a.types`.
> - **Interceptor contract fully redesigned** (auth-critical): `ClientCallInterceptor.intercept(...)` is **replaced** by `async before(BeforeArgs)` / `async after(AfterArgs)` returning `None`. There is no `http_kwargs`; inject auth by mutating `args.context.service_parameters['Authorization'] = f'Bearer {token}'` (create `ClientCallContext()` / `service_parameters = {}` if None). Reference: `a2a.client.auth.interceptor.AuthInterceptor`.
> - **`SecurityScheme` is a protobuf oneof `scheme`**: detect OIDC via `scheme.HasField('open_id_connect_security_scheme')`, then `scheme.open_id_connect_security_scheme.open_id_connect_url`. `AgentCard.security_requirements` (repeated) + `security_schemes` (map) — use truthiness, not `HasField` (raises on repeated/map).
> - **`Part`/`Message`/`Task` metadata are `google.protobuf.Struct`** but the message constructors accept a plain `dict` directly (`Message(metadata={...})`, `Part(text=..., metadata={...})`) — protobuf coerces it (ints become floats).
> - **Pydantic models holding A2A types** (`A2ATaskResponse.task: Task`, `A2AMessageResponse.message: Message`) need `model_config = ConfigDict(arbitrary_types_allowed=True)` since the types are now protobuf, not Pydantic.

## 2. Breaking Changes & Action Items

Ordered by blast radius. File references are from the current v0.3 usage map.

### 2.1 Protobuf type system replaces Pydantic  🔴 Critical

**What changed:** Wire types are Protobuf classes. No `model_dump()`, no `RootModel.root`, no arbitrary attribute assignment, no `model_validate()`. Convert with `google.protobuf.json_format.MessageToDict()` / `ParseDict()`, check optionals with `msg.HasField('x')`, build struct data with `ParseDict(d, Value())`.

**Where it bites us:**
- `agent-common/agent_common/a2a/client_runnable.py` — `Part(root=…)`, `.root if hasattr(part,"root") else part`, `inner.kind in ("text","file","data")`.
- `console-backend/console_backend/services/messages_service.py:24-41` — `_serialize_part()` does `model_dump()` and unwraps `{'root': {...}}`. **Must be rewritten for ProtoJSON.**
- `console-backend/.../models/message.py` — `list[Part]` persisted to Postgres.
- `ringier-a2a-sdk/.../utils/a2a_part_conversion.py` — `a2a_parts_to_content()` discriminates via `.root` + `isinstance`.
- `agent-common/.../authentication/interceptor.py` — reads `AgentCard.security_schemes` as `SecurityScheme(root=…)` RootModel.

**Action items:**
- [ ] Replace all `Part(root=TextPart(...))` with `Part(text=...)`; `Part(root=DataPart(data=d))` with `Part(data=ParseDict(d, Value()))`; file parts with `Part(raw=..., media_type=..., filename=...)` or `Part(url=..., media_type=..., filename=...)`.
- [ ] Replace part discrimination `isinstance(inner, TextPart/DataPart/FilePart)` and `.root.kind` with `Part.HasField('text'|'data'|'raw'|'url')`.
- [ ] Replace all `model_dump()` on A2A types with `MessageToDict(...)`; replace JSON→type parsing with `ParseDict(...)` / the SDK's ProtoJSON loaders.
- [ ] Replace `SecurityScheme(root=…)` access in the interceptor with the v1.0 protobuf accessor.
- [ ] Audit every spot that assigns ad-hoc attributes onto A2A objects (protobuf forbids this) — move that data into `metadata` or a wrapper dataclass.

### 2.2 Enum value renames (snake_case → SCREAMING_SNAKE_CASE)  🔴 Critical

**Mapping (our usages):**

| v0.3 | v1.0 |
|---|---|
| `TaskState.submitted` | `TaskState.TASK_STATE_SUBMITTED` |
| `TaskState.working` | `TaskState.TASK_STATE_WORKING` |
| `TaskState.completed` | `TaskState.TASK_STATE_COMPLETED` |
| `TaskState.failed` | `TaskState.TASK_STATE_FAILED` |
| `TaskState.canceled` | `TaskState.TASK_STATE_CANCELED` |
| `TaskState.input_required` | `TaskState.TASK_STATE_INPUT_REQUIRED` |
| `TaskState.auth_required` | `TaskState.TASK_STATE_AUTH_REQUIRED` |
| `TaskState.rejected` | `TaskState.TASK_STATE_REJECTED` |
| `TaskState.unknown` | `TaskState.TASK_STATE_UNSPECIFIED` (note: rename + semantics) |
| `Role.user` | `Role.ROLE_USER` |
| `Role.agent` | `Role.ROLE_AGENT` |

**Where it bites us:**
- `agent-common/.../a2a/models.py:181-196` — `TaskResponseData` computed fields (`is_complete`, `requires_auth`, `requires_input`) compare against `TaskState`.
- `agent-common/.../client_runnable.py:319-335`, `stream_events.py:42-44`.
- `orchestrator-agent/.../executor.py` — state-machine checks (`input_required`, `auth_required`).
- `console-backend/.../models/message.py` — default `TaskState = TaskState.unknown` and `TaskState(state_val)` parsing.
- `ringier-a2a-sdk/.../models.py` — `TaskState`→custom `TodoState` mapping.

**Action items:**
- [ ] Mechanically replace all enum references using the table above.
- [ ] **Audit DB columns** storing the enum string value: persisted rows will hold the old lowercase string. Decide: migrate stored values, or store the protobuf integer/canonical name. (See §2.7.)
- [ ] Replace `TaskState.unknown` default with `TASK_STATE_UNSPECIFIED` and review the semantics where we relied on "unknown".

### 2.3 `AgentCard` restructuring  🔴 Critical

**What changed:** `url` removed → `supported_interfaces=[AgentInterface(url=…, protocol_binding=…, protocol_version=…)]`. `supports_authenticated_extended_card` → `AgentCapabilities.extended_agent_card`. `AgentCapabilities.input_modes`/`output_modes` removed. `AgentCard.examples` → `AgentSkill.examples`. `protocol_binding` ∈ `{'JSONRPC','HTTP+JSON','GRPC'}`.

**Where it bites us:** every server `AgentCard(...)` construction — `orchestrator-agent/main.py:26-31`, `agent-creator/main.py:14-19`, `agent-runner/main.py:24-29`, `voice-agent/.../server.py:36-41` — plus card-consuming code: `console-backend` card resolver, `agent-common` interceptor and skill-description extraction (`skill.examples`).

**Action items:**
- [ ] Rewrite every `AgentCard` to use `supported_interfaces`. Declare `protocol_binding='JSONRPC'` (and add `'HTTP+JSON'` where we want it — see §4).
- [ ] Move `supports_authenticated_extended_card` → `AgentCapabilities(extended_agent_card=…)`.
- [ ] Move any `AgentCard.examples` into the relevant `AgentSkill`.
- [ ] Update card consumers that read `card.url` to read `card.supported_interfaces[*].url` (and pick by `protocol_binding`).
- [ ] **Discovery impact:** `orchestrator-agent/app/core/discovery.py` and `console-backend` `A2ACardResolver` must handle the new card shape — and, during rollout, **both** v0.3 and v1.0 cards (see §2.8).

### 2.4 Server application classes removed  🔴 Critical

**What changed (verified):** `a2a.server.apps` (`A2AStarletteApplication`, `A2AFastAPIApplication`, `A2ARESTFastApiApplication`) is **removed** (import fails). Build the ASGI app directly from route factories in `a2a.server.routes`. Verified signatures:
- `create_agent_card_routes(agent_card, card_modifier=None, card_url='/.well-known/agent-card.json') -> list[Route]`
- `create_jsonrpc_routes(request_handler, rpc_url, context_builder=None, enable_v0_3_compat=False) -> list[Route]`
- `create_rest_routes(request_handler, context_builder=None, enable_v0_3_compat=False, path_prefix='') -> list[BaseRoute]`
- `DefaultRequestHandler(agent_executor, task_store, agent_card, queue_manager=None, push_config_store=None, push_sender=None, request_context_builder=None, ...)` — **`agent_card` is a required positional (3rd arg).**

✅ **`add_a2a_routes_to_fastapi` IS available on our target (1.1.0)** — verified signature: `add_a2a_routes_to_fastapi(app: FastAPI, *, agent_card_routes=None, jsonrpc_routes=None, rest_routes=None) -> None` (keyword-only). It was **absent in 1.0.1 and added afterwards** — a concrete reason to standardize on 1.1.0. Since all four of our servers are FastAPI-based, use it directly: build the route lists with the factories and hand them to `add_a2a_routes_to_fastapi(app, agent_card_routes=…, jsonrpc_routes=…)`. (The factories also return plain Starlette `Route`s, so manual mounting via `routes=` remains a fallback.) **Good news:** `a2a.server.tasks` (`TaskStore`, `InMemoryTaskStore`, `DatabaseTaskStore`, `BasePushNotificationSender`, `InMemoryPushNotificationConfigStore`, `TaskUpdater`) and `a2a.server.agent_execution` (`AgentExecutor`, `RequestContext`, `RequestContextBuilder`) keep their **names and import paths** — so our server task-store/push imports and ringier's executor/context-builder subclass paths are unchanged.

**Where it bites us:** `orchestrator-agent/main.py`, `agent-creator/main.py`, `agent-runner/main.py`, `voice-agent/.../server.py` — all use `A2AFastAPIApplication(...).build(lifespan=...)` + `DefaultRequestHandler(executor, task_store)`.

**Action items:**
- [ ] Replace `A2AFastAPIApplication` with a `FastAPI()` app + `add_a2a_routes_to_fastapi(app, agent_card_routes=create_agent_card_routes(card), jsonrpc_routes=create_jsonrpc_routes(handler, '/'))` (add `rest_routes=create_rest_routes(handler)` if we expose HTTP+JSON). No `enable_v0_3_compat` (big-bang).
- [ ] Add `agent_card=` (3rd positional) to every `DefaultRequestHandler(...)`; pass push stores via `push_config_store=`/`push_sender=` and ringier's builder via `request_context_builder=`.
- [ ] Re-attach our `lifespan`, middleware (`ringier-a2a-sdk` JWT/user-context/steering), and custom routes to the directly-constructed app. **Verify middleware ordering** — previously layered by the wrapper's `.build()`. Bake this into the shared `build_a2a_app(...)` factory (§3.4).

### 2.5 Client config + streaming return type changed (but `ClientFactory` survives)  🔴 Critical

**Corrected against the wheel.** The docs claimed `ClientFactory` was removed — **it was not.** Both ship:
- `ClientFactory(config: ClientConfig | None).create(card, interceptors=None) -> Client` — **our exact current pattern still works.**
- `create_client(agent, client_config=None, interceptors=None, ...) -> Client` — a convenience wrapper (optional simplification, not required).

The **real** breaks are:
1. **`ClientConfig.supported_transports` and the `TransportProtocol` enum are GONE.** `ClientConfig` (still a Pydantic model) now exposes: `httpx_client`, `grpc_channel_factory`, `polling`, `streaming`, `use_client_preference`, `push_notification_config`. Our `ClientConfig(supported_transports=[TransportProtocol.jsonrpc])` and `TransportProtocol.http_json` references **will not import/construct**. Transport is now selected via the agent card's `supported_interfaces` + `use_client_preference` (and `grpc_channel_factory` for gRPC).
2. **`send_message()` returns `AsyncIterator[StreamResponse]`** (verified: `(request: SendMessageRequest, *, context=None) -> AsyncIterator[StreamResponse]`). Branch on `chunk.HasField('task'|'status_update'|'artifact_update'|'message')` instead of unpacking `(event, message)` tuples.
3. **`push_notification_configs` (list) → `push_notification_config` (single)** on `ClientConfig` (verified).

**Where it bites us:**
- `agent-common/.../client_runnable.py` — `ClientFactory(client_config).create(agent_card, interceptors=…)` (survives), but `supported_transports=[TransportProtocol.jsonrpc]` (**breaks**) and the tuple-unpacking stream loop (`task, update_event = item`; `isinstance(update_event, TaskArtifactUpdateEvent)`) (**breaks**).
- `console-backend/console_backend/utils/connection_pool.py:394-403` — `ClientFactory(a2a_config).create(agent_card)` (survives), `supported_transports=[http_json, jsonrpc]` (**breaks**), tuple-unpacking stream loop in `app.py` (**breaks**).

**Action items:**
- [ ] Remove all `supported_transports=` / `TransportProtocol` usage; drive transport via the card's `supported_interfaces` (+ `use_client_preference`). Keep `ClientFactory(config).create(card, interceptors=…)` — no need to switch to `create_client` unless we want the simpler call site (§3.5).
- [ ] Rewrite both streaming consumers to the `HasField`-based `StreamResponse` loop.
- [ ] Switch `ClientConfig` to single `push_notification_config`.
- [ ] Check for `resubscribe` usage; if present, confirm the v1.0 method name against the wheel before assuming `subscribe`.

### 2.6 `AgentExecutor` strict streaming validation  🟠 High

**What changed:** the server now **hard-errors** if an executor mixes a `Message` and a `Task` in one stream, emits multiple `Message`s, or emits an update event before the initial `Task`. Pick exactly one pattern: *Message-only* or *Task-lifecycle (Task first → updates → terminal state)*.

**Where it bites us:**
- `orchestrator-agent/app/core/executor.py` — emits status messages and `TaskStatusUpdateEvent`s via `EventQueue`/`TaskUpdater`; needs an audit to ensure it always enqueues a `Task` before any update event and never mixes a bare `Message` into task mode.
- `ringier-a2a-sdk/.../server/executor.py` (`BaseAgentExecutor`) and `agent/base.py` streaming (`_stream_impl`) — the shared streaming contract for all our agents. Must be brought into compliance once, centrally.

**Action items:**
- [ ] Audit `BaseAgentExecutor` and `OrchestratorDeepAgentExecutor` event sequences; enforce the Task-lifecycle pattern (we use tasks + HITL + streaming, so Message-only doesn't fit).
- [ ] Adopt the new helpers `new_task_from_user_message`, `new_text_status_update_event`, `new_text_artifact_update_event` to standardize emission.
- [ ] Add tests asserting no "message mode / task mode" violations.

### 2.7 `TaskStatusUpdateEvent.final` removed + push-notification config consolidation  🟠 High

**What changed:** the `final` field on `TaskStatusUpdateEvent` is **removed** — stream completion is signaled by reaching a terminal `TaskState`, not a flag. `TaskPushNotificationConfig` + `PushNotificationConfig` consolidated; `ClientConfig.push_notification_configs` (list) → `push_notification_config` (single).

**Where it bites us:** `console-backend/app.py` & `agent-common` stream loops that may read `final`; `orchestrator-agent/main.py`, `agent-runner/main.py`, `voice-agent` push-notification setup (`BasePushNotificationSender`, `InMemoryPushNotificationConfigStore`).

**Action items:**
- [ ] Remove all reliance on `.final`; detect completion via terminal `TaskState`.
- [ ] Update `ClientConfig` to single `push_notification_config`.
- [ ] Verify `BasePushNotificationSender` / `InMemoryPushNotificationConfigStore` signatures under v1.0 and the consolidated config request shape (no duplicated ID fields; `ListTaskPushNotificationConfigs` pluralized).

### 2.8 Persisted A2A data is incompatible (ProtoJSON ≠ Pydantic dump)  🟠 High

**What changed:** v1.0 serializes via ProtoJSON. v0.3 Pydantic dumps used a different shape (`{'root': {...}}` wrappers, `kind` discriminators, camelCase/snake variants, lowercase enums). Stored v0.3 payloads **will not deserialize** under v1.0.

**Scope is now narrow.** The `orchestrator-agent` `DatabaseTaskStore` is greenfield (no deployed data anywhere) — build it directly against v1.0 ProtoJSON, **no migration needed**. The only store with pre-existing data is:
- `console-backend` — `Message.parts` and `TaskState` persisted in PostgreSQL (chat history).

**Decision (made):** the console-backend chat history is **disposable** — we **clear it on cutover**. No backfill / translation code is written. This removes the last remaining data-migration risk from the whole plan.

**Action items:**
- [ ] Add a one-time cutover step that truncates the message store (or drops/recreates the parts / `TaskState` columns) as part of the v1.0 deploy.
- [ ] Ensure no read path assumes pre-cutover rows exist (empty store must be a valid state).
- [ ] No backfill, no dual-read shim, no ProtoJSON translation of legacy rows.

### 2.9 OAuth flows tightened  🟡 Medium

**What changed:** implicit and password grant flows **removed**; device-code and PKCE **added**. American spelling standardized (`canceled`). `extendedAgentCard` moved into `AgentCapabilities`.

**Where it bites us:** `OpenIdConnectSecurityScheme` / `SecurityScheme` usage in card construction and the RFC 8693 token-exchange interceptor (`agent-common/.../authentication/interceptor.py`). We use OIDC + token exchange, not implicit/password, so the risk is low — but the security-scheme model is now protobuf.

**Action items:**
- [ ] Confirm our OIDC security-scheme construction still maps cleanly (it should — we don't use implicit/password).
- [ ] Rewrite scheme field access for protobuf accessors in the interceptor (overlaps with §2.1).

### 2.10 Version pins block the upgrade  🟢 Mechanical

Every `pyproject.toml` pins `a2a-sdk ... <1.0.0`, including `ringier-a2a-sdk` itself — **except `voice-agent`, which has no upper bound at all** (`a2a-sdk[http-server]>=0.3.9`); see §6.3.

**Target version: `1.1.0` (decided, verified).** Latest published; releases were 1.0.0 → 1.0.1 → 1.0.2 → 1.0.3 → 1.1.0. The full Phase-0 introspection above was run against **1.1.0** — including the route/client surfaces the docs got wrong — so the API specifics in this plan are ground-truth for the target. Pin `>=1.1.0,<2.0.0` (or `==1.1.0`).

**Action items:**
- [ ] Set `a2a-sdk` to `>=1.1.0,<2.0.0` in: `ringier-a2a-sdk`, `agent-common`, `console-backend`, `agent-creator`, `orchestrator-agent`, `agent-runner`, **and `voice-agent`** (replacing its unbounded `>=0.3.9` — see §6.3).
- [ ] Bump `ringier-a2a-sdk`'s own version and the `>=` floors used by consumers; refresh all `uv.lock` files.

---

## 3. Refactoring & Simplification Opportunities

The v1.0 API is leaner — lean into it rather than porting v0.3 idioms 1:1.

1. **Delete the `Part(root=…)` unwrap helpers entirely.** All the `.root if hasattr(part,"root") else part` and `_serialize_part()` `{'root': {...}}`-unwrapping logic (`messages_service.py:24-41`, `client_runnable.py`, `a2a_part_conversion.py`) exists *only* to deal with the RootModel wrapper. With flat `Part`, this collapses to direct `Part.HasField(...)` branching. Net deletion of code.

2. **Centralize part↔content conversion in `ringier-a2a-sdk`.** We currently convert parts in at least three places (agent-common client, console-backend service, ringier util). Make `a2a_parts_to_content()` (and a reverse `content_to_a2a_parts()`) the single canonical converter in `ringier-a2a-sdk` and have every package import it. The migration is a forcing function to dedupe.

3. **Adopt `a2a.helpers` and retire bespoke builders.** `new_text_message`, `new_message`, `new_task_from_user_message`, `new_text_status_update_event`, `new_text_artifact_update_event`, `get_message_text`, `get_text_parts`, `get_artifact_text` replace most of our hand-rolled message/event construction in `executor.py`, `a2a_extensions.py`, and `models.py`. Less boilerplate, spec-correct emission.

4. **Standardize server bootstrap.** All four servers (`orchestrator`, `agent-creator`, `agent-runner`, `voice-agent`) repeat near-identical `A2AFastAPIApplication` setup. Replace with one shared `build_a2a_app(card, handler, *, middleware, lifespan)` factory in `ringier-a2a-sdk` wrapping `add_a2a_routes_to_fastapi`. Single place to evolve transport/route config.

5. **Standardize client bootstrap.** `await create_client(...)` + a shared `ClientConfig` builder (interceptors, transports, push config) replaces the duplicated `ClientFactory` wiring in `agent-common` and `console-backend`. One `connection_pool`-friendly client factory.

6. **Simplify the streaming consumer.** The new `StreamResponse` + `HasField` loop is flatter than tuple-unpacking with `isinstance` ladders. Collapse the two stream consumers into one shared iterator that yields normalized events.

7. **Drop the `final`-flag bookkeeping.** Completion = terminal `TaskState`. Remove any state we tracked to know when a stream ends.

---

## 4. New Features to Evaluate

1. **HTTP+JSON (REST) transport.** v1.0 first-classes a REST binding (`create_rest_routes`, `add_a2a_routes_to_fastapi`). HTTP+JSON is now the *preferred* default in some SDKs (e.g. Microsoft's). Evaluate exposing HTTP+JSON alongside JSON-RPC on our agents for easier debugging, proxy/CDN friendliness, and broader client compatibility — declare both via `supported_interfaces`.

2. **gRPC transport.** Now a fully-supported, spec-mapped binding. For high-throughput internal agent↔agent calls (orchestrator → sub-agents) gRPC could reduce overhead. Evaluate as an internal-only interface (keep JSON-RPC/HTTP+JSON public).

3. **`tasks/list` with filtering + pagination.** New native method. Could replace/augment any custom task-listing we do in `console-backend` and simplify the orchestrator's view of in-flight tasks.

4. **Server-managed Task IDs.** v1.0 makes task ID generation explicitly server-side. Lets us drop any client-side ID minting and rely on the server contract.

5. **Native multi-tenancy scope on gRPC.** New per-request scope field. Relevant to our zero-trust / per-user-sub model in `ringier-a2a-sdk` middleware — could carry tenant/user scoping at the protocol layer instead of only in JWT/headers.

6. **PKCE + device-code OAuth.** Security upgrade. If any client runs in a browser/CLI context, adopt PKCE; device-code helps headless onboarding. Aligns our OIDC schemes with current best practice.

7. **`enable_v0_3_compat` bridge.** ❌ **Out of scope.** This exists to let a v1.0 server serve not-yet-upgraded v0.3 clients during a phased rollout. We are doing a big-bang monorepo deploy, so we deliberately **do not** enable it — no dual interfaces, no compat flag, no mixed-version window. (Noted here only to record the conscious decision *not* to use it.)

8. **`display_agent_card` helper.** Minor DX win for ops/debug tooling and tests.

---

## 5. Step-by-Step Execution Plan

Sequenced to respect the dependency graph: **`ringier-a2a-sdk` is the foundation** every other package imports, so it migrates first. Use the `enable_v0_3_compat` bridge so the fleet can roll over incrementally rather than in one big-bang deploy.

### Phase 0 — Spike & guardrails (de-risk)
1. ✅ **Done** — Phase-0 spike completed: target set to `a2a-sdk==1.1.0`, real import surface introspected and reconciled into this plan (§1 banner, §2.4, §2.5). Create the migration branch.
2. Read the SDK's official guide and DB notes end-to-end: `a2aproject/a2a-python` → `docs/migrations/v1_0/README.md` and `docs/migrations/v1_0/database`.
3. Build a tiny throwaway v1.0 server + client to validate: ProtoJSON (de)serialization, `create_client`, route factories, and `enable_v0_3_compat` against one of our existing v0.3 agents. Confirm the compat bridge actually works for our card shapes.
4. Inventory all persisted A2A data and enum-string columns (console-backend message store; orchestrator `DatabaseTaskStore`).

### Phase 1 — Migrate `ringier-a2a-sdk` (the foundation)  🔴
5. Bump `a2a-sdk` to `>=1.1.0,<2.0.0` in `ringier-a2a-sdk/pyproject.toml`.
6. Rewrite `utils/a2a_part_conversion.py` for flat `Part` + `HasField` (delete `.root` unwrapping). Add the reverse converter (§3.2).
7. Update `models.py` enum references (`TaskState`/`Role`) and the `TaskState`→`TodoState` map.
8. Bring `server/executor.py` (`BaseAgentExecutor`) and `agent/base.py`/`langgraph.py` streaming into strict Task-lifecycle compliance (§2.6); adopt `a2a.helpers`.
9. Update `server/context_builder.py` (`AuthRequestContextBuilder` subclass of `RequestContextBuilder`) and the middleware suite for any protobuf/context API changes.
10. Add the shared `build_a2a_app(...)` server factory and shared `create_client`/`ClientConfig` builder (§3.4, §3.5).
11. Verify the custom **extensions** mechanism (`Message.extensions` + `DataPart`-style structured metadata: activity-log, work-plan, HITL) still works — `extendedAgentCard` moved into `AgentCapabilities`, so confirm `Message.extensions` survives and structured data round-trips via `data=ParseDict(...)`.
12. Green the `ringier-a2a-sdk` test suite. **This is the gate for everything downstream.**

### Phase 2 — Migrate the leaf agent servers  🔴
Do these in parallel once Phase 1 lands (each only depends on `ringier-a2a-sdk` + `agent-common`):
13. **`agent-common`** first (shared client + models): rewrite `client_runnable.py` (`create_client`, `StreamResponse` loop), `models.py` (`TaskResponseData` enum comparisons), `authentication/interceptor.py` (protobuf `SecurityScheme` access), `factory.py`, `stream_events.py`.
14. **`agent-creator`**, **`agent-runner`**, **`voice-agent`**: swap `A2AFastAPIApplication` → shared `build_a2a_app`; add `agent_card=` to `DefaultRequestHandler`; rewrite `AgentCard` to `supported_interfaces`; fix push-notification config; declare both `JSONRPC` (and optionally `HTTP+JSON`) interfaces.
15. **`orchestrator-agent`**: same server changes plus rewrite `executor.py` (strict streaming, enum renames, part construction), `a2a_extensions.py` (extension builders), `discovery.py` (consume new + v0.3 card shapes), and the `DatabaseTaskStore` usage.

### Phase 3 — Migrate `console-backend` (+ message-store decision)  🔴
16. Rewrite `connection_pool.py` / `app.py` client wiring (`create_client`, `StreamResponse` loop) and `A2ACardResolver` handling for the v1.0 card shape (v1.0-only — no v0.3 fallback).
17. Rewrite `messages_service.py` `_serialize_part()` for ProtoJSON; update `models/message.py` `Part`/`TaskState` fields.
18. Add the message-store **truncation** step to the cutover (chat history is disposable — §2.8): clear/recreate the parts & `TaskState` columns on deploy. No backfill, no `DatabaseTaskStore` migration (greenfield).

### Phase 4 — Big-bang release  🟠
19. Land all package migrations on the branch together. Every `AgentCard` advertises a single **v1.0** `AgentInterface` (`JSONRPC`, optionally `HTTP+JSON`) — **no** `'0.3'` interface, **no** `enable_v0_3_compat`.
20. Run the full cross-package integration suite against the v1.0 fleet (orchestrator ↔ sub-agents, console-backend ↔ agents, Slack/Google-Chat clients).
21. Update `example-k8s-deployment` manifests / env (the orchestrator manifest is already modified on this branch — reconcile) and cut **one coordinated release** across all packages; deploy the whole fleet together.
22. **Platform note:** if any agent card is cached platform-side (e.g. Gemini Enterprise), refresh it so the new v1.0 interfaces are picked up.

### Phase 5 — Cleanup & adopt new features
23. Delete the old part-unwrap helpers and `final`-flag bookkeeping.
24. Evaluate and, where justified, adopt: HTTP+JSON/gRPC interfaces (§4.1–4.2), `tasks/list` (§4.3), multi-tenancy scope (§4.5), PKCE (§4.6).
25. Refresh `AGENTS.md`/`CONTEXT.md` docs and `uv.lock`s.

### Cross-cutting checklist
- [ ] All `a2a-sdk` pins → `>=1.1.0,<2.0.0`; all `uv.lock`s refreshed.
- [ ] Grep gates that must return zero hits post-migration: `Part(root=`, `.root`, `TextPart|DataPart|FilePart` (as constructors), `TaskState.completed|working|failed|...` (lowercase), `Role.user|Role.agent`, `A2AFastAPIApplication|A2AStarletteApplication`, `ClientFactory`, `\.final`, `AgentCard(.*url=` (top-level), `model_dump()` on A2A types, `enable_v0_3_compat`.
- [ ] Contract/integration tests cover v1.0↔v1.0 across all inter-agent and client paths (no v0.3 compat path to test — big-bang).

---

## 6. Package Dependency & Release Orchestration

A breaking `ringier-a2a-sdk` change cascades through every internal consumer, so the version bumps, constraint tightening, and release must be choreographed. The good news: the repo's existing release system (`scripts/release-helpers.sh` + `just release`) is already built for this — but two of the moves below are **manual** and easy to miss.

### 6.1 Internal dependency graph

```
a2a-sdk (external, → 1.1.0)
   └── ringier-a2a-sdk        (lib, released, tagged)
          ├── agent-common    (lib, NOT independently released — vendored)
          │      ├── agent-creator      (service)
          │      ├── agent-runner       (service)
          │      └── orchestrator-agent (service)
          ├── console-backend (service)
          └── voice-agent     (service)
   client-slack / client-google-chat / client-email / *-frontend  (Node — talk to services over A2A wire; see 6.6)
```

Key facts from the build/release tooling:
- **Per-package semver**, version in `pyproject.toml`; git tags `<package>/v<version>`. Release order in `ALL_PACKAGES` is **already topological** (`ringier-a2a-sdk` first → services → clients) — do not reorder.
- **`agent-common` and `object-storage` are NOT independently released.** They are vendored into each service image via Docker named build contexts (`COPY --from=agent-common .`, `COPY --from=ringier-a2a-sdk .`). So their changes ride along inside every consuming service's image rebuilt from the same commit.
- **Builds use the local source, not a registry** (`uv sync --frozen` over copied sibling packages). Therefore the `>=` version constraints are **not enforced at build time** — but `uv.lock` **is** (`--frozen`), so any constraint change forces a lock refresh or the build fails.
- **`just release` auto-bumps from Conventional Commits**: a `type!:` subject or `BREAKING CHANGE:` footer → **major**; `feat` → minor; else patch. It bumps the `version =` line only — **it does not rewrite dependency constraints.**

### 6.2 Version bump decisions

| Package | Current | Recommended | Rationale |
|---|---|---|---|
| `ringier-a2a-sdk` | 0.15.2 | **1.0.0** | Breaking API rewrite; aligning to A2A 1.0 makes the major signal unambiguous. |
| `agent-common` | 0.7.0 | bump (e.g. 0.8.0) | Public surface (part conversion, models) changes; vendored, so the number is mostly documentation — but bump it for traceability. |
| `console-backend` | 0.26.1 | major-level | Breaking protocol/storage change. |
| `orchestrator-agent` | 0.26.0 | major-level | Breaking executor/card/store change. |
| `agent-creator` | 0.18.1 | major-level | Breaking server/card change. |
| `agent-runner` | 0.20.1 | major-level | Breaking server/card change. |
| `voice-agent` | 0.13.1 | major-level | Breaking server/card change. |

> **0.x note:** these services are pre-1.0, where breaking changes are conventionally a *minor* bump — but this repo's tooling maps `!:`→**major** regardless. Decide once: either accept the jump to `1.0.0` across services (clean "now requires A2A 1.0" signal, recommended) or force `just release minor`. Be consistent.

### 6.3 Constraint tightening — the manual step the tooling won't do

Edit these by hand as part of the migration PR (the release script only bumps `version=`):

- **`a2a-sdk` pin — everywhere:** `>=0.3.25,<1.0.0` → `>=1.1.0,<2.0.0` in `ringier-a2a-sdk`, `agent-common`, `console-backend`, `agent-creator`, `agent-runner`, `orchestrator-agent`. **`voice-agent` is the live hazard — confirmed by the spike:** its constraint is `a2a-sdk[http-server]>=0.3.9` with *no upper bound*, and the only thing keeping it on v0.3 is its **lockfile, frozen at `a2a-sdk==0.3.26`**. Any `uv lock` regeneration (which this migration forces) would silently jump it to the latest **1.1.0** — uncontrolled. Pin it explicitly to `>=1.1.0,<2.0.0`, same as every other package; **voice-agent is a full first-class participant in the big-bang** (server + card + client migration), not merely a constraint fix.
- **`ringier-a2a-sdk` floor — every consumer:** `>=0.1.0` (and voice-agent's `>=0.7.0`) → `>=1.0.0,<2.0.0` so a consumer can never resolve a pre-v1 SDK.
- **`agent-common` floor — its consumers:** raise `>=0.7.0`/`>=0.1.0` to the new `agent-common` version.

These floors are documentation-grade given the path-based builds, but tightening them prevents a future stray `pip install` from pulling an incompatible version and records the hard boundary.

### 6.4 Lockfiles

- After editing constraints, **regenerate every affected `uv.lock`** (`uv lock` per package). The Docker build runs `uv sync --frozen`; a stale lock = failed build. This is non-optional.
- Commit the refreshed locks in the same PR.

### 6.5 Cutting the release (big-bang, atomic)

1. Land all of: code migration (Phases 1–3), §6.3 constraint edits, §6.4 lock refreshes — on the migration branch, merged to `main`.
2. Drive the bump level. Two options:
   - **Conventional commits:** write the migration commits with `feat(a2a)!: migrate to a2a-sdk v1.0` / `BREAKING CHANGE:` footers so `just release` auto-selects major for the changed packages.
   - **Forced level:** run `just release major` (or `minor` per the §6.2 decision) to bump all changed packages uniformly.
3. `just release` walks `ALL_PACKAGES` in topological order, bumps each changed package, writes **one** `release: …` commit, creates per-package tags, then builds & pushes all Docker images.
4. **Atomicity:** because every service image vendors its internal deps from the *same commit* via build contexts, the whole fleet is mutually consistent by construction — there is no registry-publish ordering race and no window where a v1.0 service could pull a v0.3 lib. This is exactly the property that makes the big-bang safe here.

### 6.6 Don't forget the Node clients

`client-slack`, `client-google-chat`, `client-email` and the frontends are in `ALL_PACKAGES` but are **not** Python and don't use `a2a-sdk`. They are only affected if they construct/parse A2A wire payloads directly (new `Part`/`AgentCard` shapes, `SCREAMING_SNAKE_CASE` enums) rather than going through `console-backend`.

- [ ] Verify whether any Node client speaks the A2A wire format directly. If yes, it joins the big-bang (update its payload shapes + bump it); if it only talks to `console-backend`'s own API, it's unaffected by the SDK change.

### 6.7 Orchestration checklist

- [ ] §6.3 constraint edits applied in all 7 Python `pyproject.toml`s (incl. voice-agent's missing upper bound).
- [ ] All affected `uv.lock`s regenerated and committed.
- [ ] Bump level decided (§6.2) and encoded via commit convention or `just release <level>`.
- [ ] `just release` run once → single release commit + per-package tags in topological order + all images built/pushed.
- [ ] Node clients audited (§6.6).

---

## Sources

- [A2A Python SDK v1.0 migration guide](https://github.com/a2aproject/a2a-python/blob/main/docs/migrations/v1_0/README.md) (and `docs/migrations/v1_0/database`)
- [A2A SDK v1 Migration Guide — Microsoft Learn](https://learn.microsoft.com/en-us/agent-framework/migration-guide/agent-to-agent-sdk-v1)
- [A2A v1.0.0 release notes — a2aproject/A2A](https://github.com/a2aproject/A2A/releases/tag/v1.0.0)
- [The A2A 1.0 Milestone: Ensuring and Testing Backward Compatibility — Google Cloud (Medium)](https://medium.com/google-cloud/the-a2a-1-0-milestone-ensuring-and-testing-backward-compatibility-4aadb007c49b)
- [What's new in v1 — a2acn.com](https://a2acn.com/en/docs/community/whats-new-v1/)

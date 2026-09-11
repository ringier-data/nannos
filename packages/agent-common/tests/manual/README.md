# Manual QA cases

Scripts here are **not** collected by pytest. They are run by hand, by a person, to
reproduce a specific defect or to verify a specific fix.

---

## QA-217 — two concurrent `eval` calls on one thread collide

Issue: [#217](https://github.com/ringier-data/nannos/issues/217)
Script: [`qa_217_parallel_eval.py`](qa_217_parallel_eval.py)

### What is under test

Nothing guarantees a model step emits at most one `eval` tool call, but every
piece of per-`eval` bookkeeping on the PTC path is keyed by `thread_id` alone.
When two `eval` calls land in one assistant message, `ToolNode` runs them
concurrently (`asyncio.gather`) and they stomp each other at three layers:

| Layer | Where | What collides |
|---|---|---|
| 1 | `langchain_quickjs` REPL slot | Both evals get the *same* QuickJS context from `_repl_for_eval(thread_id)`; in `mode="call"` the first to finish runs `reset_repl(thread_id)` in its `finally`, closing the context the other is still executing in. |
| 2 | `ptc_guard._PTC_TURNS` | `begin_ptc_turn(thread_id)` *replaces* the thread's turn, so eval B swaps the turn out from under eval A — shared `results`/`decisions`/`pending`, and a `tool_call_history` write-back computed against a stale seed (last writer wins). |
| 3 | HITL batching | `take_ptc_pending(thread_id)` drains one collector for the thread, so approvals from two evals land in whichever `interrupt()` fires first. |

### Why it is scripted, not prompted

The trigger is a model behaviour nobody can request reliably: *two `eval` calls in
one assistant message*. The script uses a scripted `BaseChatModel` to force exactly
that, while the whole PTC stack underneath — `_PTCToleranceCodeInterpreterMiddleware`,
the `ptc_guard` wrapper, `RepeatedToolCallMiddleware`, a real QuickJS sandbox — is
the production one. No LLM gateway, no network, no credentials.

### Preconditions

- `packages/agent-common` dependencies installed (`uv sync`).
- Nothing else. Do **not** configure a gateway; the scenarios are offline.

### Steps

```bash
cd packages/agent-common
uv run python tests/manual/qa_217_parallel_eval.py                # all three scenarios
uv run python tests/manual/qa_217_parallel_eval.py sequential     # one scenario
QA217_TRACEBACK=1 uv run python tests/manual/qa_217_parallel_eval.py   # full tracebacks
```

Each scenario runs two `eval` programs. Each one tags a REPL global, awaits its
inner `probe` tool, then reads the global back — so a shared sandbox shows up
either as a crash or as one program seeing the other's tag.

| Scenario | Setup |
|---|---|
| `sequential` | **Control.** Same two programs, one `eval` per assistant message. |
| `parallel-repl` | Both `eval` calls in one message, inner calls low-risk (score 0.1). Exercises layer 1. |
| `parallel-hitl` | Both `eval` calls in one message, inner calls high-risk (score 0.95), resumed with approve-all. Exercises layers 2 and 3. |

Eval A's `probe` sleeps 400 ms; eval B's returns immediately. The delay does not
cause the bug — it only removes the coin flip over which eval finishes first.

### Expected result (fixed build)

```
  sequential       PASS
  parallel-repl    PASS
  parallel-hitl    PASS
```

exit code `0`. Concretely, for every scenario:

- both `eval` results contain their own `ok:A` / `ok:B`,
- `ownerAfterAwait` matches the program's own tag (no shared sandbox),
- the inner tool ran exactly once per program (`['A', 'B']`),
- `tool_call_history['eval:probe']` holds **2** hashes (both write-backs survived),
- and for `parallel-hitl`, **2** approval action requests were raised in total —
  printed as `2 (action_requests each: [1, 1])`: one interrupt per eval, each
  carrying its own call, rather than one interrupt that swallowed both.

### Actual result before the fix (verified 2026-09-11 on `origin/main` @ `1c422039`)

```
  sequential       PASS
  parallel-repl    RAISED ValueError: already closed
  parallel-hitl    RAISED InvalidHandleError: handle's owning context is closed

#217 reproduced — not clean: parallel-repl, parallel-hitl
```

exit code `1`.

Both exceptions are the same root cause — the wasmtime store behind the QuickJS
context torn down mid-execution by the other eval's `reset_repl`. **Which one
surfaces depends on race timing**, so treat any `already closed` /
`InvalidHandleError` / `owning context is closed` here as this bug.

It is raised **out of the tool node**, so the whole agent turn dies — in production
a user-visible hard failure of the conversation, not a degraded tool result.

The `sequential` control passing is what rules out an environment or harness fault:
identical programs, identical tools, only the number of `eval` calls per model step
differs.

### One correction to the issue text

#217 says the `tool_call_history` write-back is "last writer wins, since the state
channel replaces the dict". It is not, and the truth is worse. With the REPL
collision fixed but no reducer, both eval tasks write that channel in one superstep
and LangGraph's default `LastValue` **raises**:

```
InvalidUpdateError: At key 'tool_call_history': Can receive only one value per step.
```

killing the turn rather than silently dropping a record. That is why the fix needs
`merge_tool_call_history` as well as the lock.

### Known noise (not failures)

- `parallel-hitl` logs `NoDefaultModelError: No default chat model is configured`
  from the tool-call summarizer. Expected — no model is configured here, so the
  approval prompt falls back to raw args. It does not affect the verdict.

### What the fix is

Two parts, both required — each was disabled alone and the harness re-run to confirm:

- **`ptc_guard.serialized_eval`** — a per-`(event loop, thread_id)` mutex held across
  the whole `begin_ptc_turn` → `end_ptc_turn` span in
  `_PTCToleranceCodeInterpreterMiddleware.awrap_tool_call`. Only the `eval` path
  enters it, so every other tool keeps its concurrency. Disable this alone and both
  parallel scenarios go back to `already closed`.
- **An incremental write-back** — with the sandbox collision gone, both eval tasks
  write the `tool_call_history` channel in one superstep. The eval path now emits a
  tagged *delta* (`history_delta`: what this eval appended, plus the window to apply
  after) instead of a whole history, and `merge_tool_call_history` appends it.
  Disable this alone and both parallel scenarios raise `InvalidUpdateError`.

The delta exists because the merge has to know *what kind* of update it is getting,
and that is not decidable from the lists alone. A first attempt tried to recognise a
window trim by comparing shapes — "the trimmed update is shorter, so take it". That
is false: `RepeatedToolCallMiddleware.evaluate` appends **then** trims, so a
saturated update is an equal-length *rotation* (`old[1:] + [new]`), which looks
exactly like two writers that diverged at the head. Every ordinary step fell into the
concatenate branch and the history grew by a full window per step — unbounded, with
duplicated hashes inflating repeat counts until legitimate calls got blocked. Tagging
the update fixes this by construction: the model-boundary writer keeps plain
whole-value replace semantics and is untouched, and only the eval path opts in.

A gotcha worth knowing if you ever touch that annotation: LangGraph detects a reducer
as `callable(metadata[-1])`, so it must be the **last** entry —
`Annotated[T, PrivateStateAttr, merge_tool_call_history]`. Put it first and it is
silently ignored, with no error and no reduction.

### The HITL side

Once the two evals stop sharing a collector they each raise their **own** interrupt,
so a turn can end with two approvals pending. The orchestrator used to render only
`interrupts[-1]` while the resume path replicates a blanket `approve` across every
pending interrupt — meaning approving the card you saw also authorised the call you
did not. `AgentStreamResponse.interrupt_value` now folds co-pending approval asks
into one card (each request keeps its own `_call_id`, so decisions still route back
correctly). Auth and client-action pauses are never folded in; they need their own
round trip.

### Verifying a future change

Run all three scenarios; every one must PASS and the exit code must be `0`. Note
that this script scripts the assistant message directly, so it bypasses the model
boundary: a fix that only stopped the *model* from emitting two `eval` calls (e.g.
`parallel_tool_calls=False`) would not be exercised here, and these scenarios would
keep failing by design. They test the layer below that.

# Single-source streaming: emission & reconstruction

Status: proposed
Scope: `orchestrator-agent` (emission), `console-backend` (persistence), `console-frontend` (reconstruction)
Not in production yet — **no data migration required**.

## Problem

The final answer of a turn has repeatedly rendered duplicated or fragmented, and we've patched it four times in different places:

| incident | layer | symptom |
|---|---|---|
| PR #67 | orchestrator stream | answer streamed twice (free-text content **and** `FinalResponseSchema.message`) → doubled live bubble |
| google-chat (79b75a6) | transport | terminal fallback joined onto streamed artifacts → "answer answer" |
| PR #68 (closed) | console persistence | terminal fallback persisted **and** streamed artifact persisted → answer stored twice on reload |
| PR #68 (closed) | console persistence | first artifact chunk (`append=False`) persisted standalone → answer **split** into two messages on reload |
| reload thought split | console reconstruction | orchestrator reasoning rendered as two "Thinking" blocks on reload but one live |

Each was patched locally. They are the same defect surfacing in different consumers.

## Root cause: two architectural seams where two paths produce the same content

1. **Dual emission.** The orchestrator emits the answer **twice**: as streamed artifact chunks *and* re-sent in the terminal `status.message` (tagged `final_answer_source: "fallback"`) for non-streaming clients. Every consumer must then dedupe — and each omission is a duplicate.

2. **Dual reconstruction.** The display (answer + thoughts + activity timeline) is assembled by **two different assemblers**:
   - **live:** the frontend accumulates socket *events* into thoughts/messages (by agent continuity, thought boundaries, interleaved activity);
   - **reload:** the backend pre-assembles chunks into messages at turn-end, and the frontend renders one thought per message (`reconstructTimelineFromMessage`).

   The two assemblers must agree to keep live == reload, and they keep drifting (first-chunk leak, per-agent merge vs. per-thought split, lost per-chunk timestamps).

**Principle:** never have two paths produce the same content. Produce it once; reconstruct it once.

## Design

### A. Single-source reconstruction (the bigger win)

The console-backend conversation history is the **web client's own store** — slack/google-chat have their own platform history, and the agent's multi-turn memory is the LangGraph checkpointer, not this. So the web client can own the format end-to-end.

- **Backend persists display events faithfully.** Stop pre-merging chunks into one message per agent at turn-end. Persist the streamed events (artifact-update / status-update) with their **original timestamps** and boundaries (the messages already carry `raw_payload`; this is mostly *stopping* the pre-merge, not new infra).
- **One frontend accumulator for live and reload.** Retire the separate grouping in `reconstructTimelineFromMessage`; feed persisted events through the **same** accumulator the live socket path uses. Then live == reload **by construction** — including the answer, thoughts, and activity interleaving.
- **Denormalized preview only.** Keep a small "last message" field on the conversation for the list view; derive it at turn-end. It is not a second assembler — just a snippet.

### B. Single-source emission

- The orchestrator emits the answer in **exactly one channel**. If it streamed the answer, the terminal `status-update` is a **bare completion signal** (state only, no message). If it did not stream (direct answer / `include_subagent_output`), the terminal carries the message.
- Removes the `final_answer_source: "fallback"` flag and every per-consumer dedup (google-chat, console persistence, reconstruction).
- Trade-off: loses the "dropped SSE frame" safety net. Acceptable — the canonical artifact is server-persisted and recoverable on reload/get-task, and the recurring duplication is the larger, certain cost.

## Non-goals / constraints

- **No migration.** Not released to prod; old-thread compatibility is out of scope.
- **Other transports keep their own rendering.** Slack/google-chat consume the final answer only; they are unaffected by the console history format.
- **Rich-timeline rendering becomes the web client's responsibility.** Acceptable — it is the only rich consumer today.

## Plan (phased, each shippable)

1. **Emission (orchestrator):** stop re-sending the answer in the terminal status when streamed; emit a bare completion. Drop the fallback flag.
2. **Persistence (console-backend):** persist display events faithfully (timestamps + boundaries); stop the per-agent/turn-end pre-merge; add the conversation preview field.
3. **Reconstruction (console-frontend):** unify live + reload onto one accumulator; remove the divergent `reconstructTimelineFromMessage` grouping.

Phases 1 and 2/3 are independent and can land separately; together they remove the whole duplication/fragmentation class.

## Risks

- Frontend reconstruction refactor is the riskiest piece (interrupt/auth/HITL/feedback timelines must still render). Verify against the same turn matrix used for the get_state work: happy / auth / HITL / phantom / feedback.
- The conversation-list preview must stay correct once the answer is no longer a standalone message.

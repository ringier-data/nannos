# Spike: should the voice agent migrate to the LiteLLM gateway?

**Assessment spike — throwaway.** No production code here. The question:
should `packages/voice-agent` stop talking to Gemini Live directly (via the
`google.genai` SDK over Vertex AI) and instead route its realtime session
through the LiteLLM proxy pod, as every other LLM call in the platform now does
(ADR-0001)?

Trigger docs:
- Vertex realtime (the path that matches this agent): <https://docs.litellm.ai/docs/providers/vertex_realtime>
- Google AI Studio realtime (the API-key variant, **not** what we run): <https://docs.litellm.ai/docs/providers/google_ai_studio/realtime>

Verdict format: each check resolves to **GO** (works, no loss), **GATE**
(works only with extra work — quantify it), or **NO-GO** (blocked / unacceptable
loss). Record results in `SPIKE-FINDINGS.md`.

---

## Why even consider it

The rest of the platform routes all LLM traffic through the LiteLLM proxy to get
(ADR-0001/0002): credentials out of app pods, centralized cost capture +
attribution, runtime model management, and unified observability. The voice
agent is the **only** service still holding cloud credentials (`GCP_KEY`) and
calling a provider SDK directly — so its realtime audio spend is **not** captured
by the gateway's `custom_logger`/SpendLogs, and its model is pinned at deploy
time. Folding it into the gateway would close that gap.

That is the entire upside. Everything below is about whether the realtime path
can actually deliver it without breaking the agent.

## What the agent uses today (the migration surface)

Inventory from [`voice_agent/agent.py`](../../packages/voice-agent/voice_agent/agent.py).
The agent speaks the **Gemini Live protocol** end to end. A migration replaces
that with LiteLLM's `/v1/realtime` passthrough, which speaks the **OpenAI
realtime protocol**. Every item below is something that must survive the
protocol translation:

| Feature in use | Where | Survives OpenAI-realtime translation? |
|---|---|---|
| Vertex AI auth (GCP service account, `europe-west1`) | `build_gemini_client` | **✅ confirmed by Vertex doc** — `vertex_project`/`vertex_location`, SA via `GOOGLE_APPLICATION_CREDENTIALS`, `europe-west1` listed. Check 1 (verify in practice). |
| **Output** audio transcription | `output_audio_transcription` → `output_transcript` events | **✅ Vertex doc** — supported, LiteLLM injects `outputAudioTranscription: {}` automatically. Check 2. |
| **Input** audio transcription | `input_audio_transcription` → `input_transcript` events | **⚠️ NOT in the Vertex feature table** (only *output* is listed). Possible loss. Check 2 (**gate**). |
| Tool calling (MCP `FunctionDeclaration`s, `tool_call`, `send_tool_response`) | `_init_mcp_tools`, `_dispatch_tool_call` | ✅ with `gemini_live_defer_setup: true` + tools in first `session.update`. Check 3. |
| Tool-response **scheduling hints** `INTERRUPT` / `WHEN_IDLE` | `_dispatch_tool_call` | Gemini-native field; no OpenAI-realtime equivalent. Likely lost. Check 4. |
| Barge-in / interruption (`sc.interrupted` → `interrupted` event) | `_receive_loop` | Server VAD ✅ in Vertex doc; OpenAI protocol has its own interruption events, needs remap. Check 4. |
| Context-window compression (sliding window) | `build_live_config` | Sent via `session.update`, but **"session.update is not forwarded"** (Vertex accepts one setup msg) — likely lost. Check 4. |
| `send_realtime_input` PCM 16 kHz in / 24 kHz out, `audio_stream_end` | `_send_loop` | ✅ Vertex doc states 16 kHz PCM16 in / 24 kHz out — exact match. Maps to OpenAI `input_audio_buffer`. Check 3. |
| Text injection as a user turn (`send_client_content`) | `_send_loop` | Maps to `conversation.item.create`. Note **`response.create` is silently ignored** (Vertex auto-responds per turn). Check 3. |
| Prebuilt voice selection (`Kore`, …) | `build_live_config` | Voice name passthrough differs between protocols. Check 3. |

## Known facts from the Vertex realtime doc

The earlier draft of this spike read the **AI Studio** page and flagged "no
audio transcription" + "Vertex support unknown" as likely blockers. The
**Vertex** page (the path that actually matches us) clears both:

- **Vertex is a first-class realtime backend.** `vertex_ai/gemini-live-2.5-flash-native-audio`
  is supported (the exact model we run), via `vertex_project` / `vertex_location`
  and a service account (`GOOGLE_APPLICATION_CREDENTIALS`); `europe-west1` is a
  listed region. Audio specs match exactly: 16 kHz PCM16 in, 24 kHz out.
- **Output transcription is supported** and applied automatically (LiteLLM injects
  `outputAudioTranscription: {}`). **Input transcription is not listed** — the open
  question now (Check 2).
- **`gemini_live_defer_setup: true`** required for tool calling (tools in the first
  `session.update`).
- **One setup message per connection** — "session.update is not forwarded" after
  the first. So everything Gemini-native we set via config (voice, system prompt,
  tools, context-window compression) must go in that one setup, and anything LiteLLM
  doesn't translate is silently dropped.
- **`response.create` is silently ignored** — Vertex auto-responds after each turn.
- The client speaks the **OpenAI realtime protocol**; the SDK entry point
  (`litellm._arealtime`) is **experimental**.

## Checks to run

Spin up a local LiteLLM proxy (reuse `../litellm-proxy-verification/docker-compose.yml`
as a base, pin the exact image digest) with a `mode: realtime` model, then drive
it with a minimal OpenAI-realtime WS client. **Pin the LiteLLM version/digest you
test — realtime is fast-moving and experimental.**

- Tested LiteLLM version/digest: `__________` (fill in)

1. **Auth / region (confirm).** Doc says Vertex + SA + `europe-west1` works —
   stand it up and confirm a session actually connects with our service account.
   Low risk now; was the biggest unknown.
2. **Transcription — input side (gate).** Output transcription is documented as
   automatic; confirm `output_transcript` events still arrive. The real question is
   **input** transcription (not in the feature table): does the user-side transcript
   survive? If not, the call transcript is half-blind → **NO-GO** unless re-derived
   (separate STT pass = added cost/latency — quantify it).
3. **Protocol parity.** Re-implement the happy path against OpenAI-realtime:
   audio in/out, voice selection, system prompt, text injection, one MCP tool call
   end to end. Record what each Gemini-Live call maps to.
4. **Feature loss.** Confirm whether scheduling hints (`INTERRUPT`/`WHEN_IDLE`),
   barge-in semantics, and context-window compression survive. List each as
   keep / remap / lost.
5. **Cost capture (the actual prize).** Confirm a realtime session produces a
   SpendLog row with usage + attribution via `custom_logger`. **If realtime
   passthrough bypasses the logger, the migration buys almost nothing** (ADR-0002) —
   verify before any other work.

## Preliminary desk assessment (pre-run, to be confirmed/refuted)

The Vertex doc moves this from "leaning NO-GO" to **plausible but unproven** —
technically supported, with the verdict now resting on two questions, not five:

1. **Cost capture (the prize, Check 5).** Cost capture is the *only* reason to
   migrate. If the realtime WS passthrough skips `custom_logger`/SpendLogs, the
   migration costs a full protocol rewrite and gains nothing. **Verify this first** —
   it gates whether the spike is even worth continuing.
2. **Input transcription (Check 2).** Output is documented; input is not. If the
   user-side transcript is lost, that's a shipped-feature regression.

Two costs are now confirmed by the doc and should be priced in regardless:
- **Feature loss** (Check 4): scheduling hints (`INTERRUPT`/`WHEN_IDLE`) and
  context-window compression are Gemini-native and won't survive the one-setup,
  OpenAI-protocol passthrough. The agent leans on both today.
- **Rewrite**: ~900 lines of `agent.py` move from the `google.genai` Live protocol
  to an OpenAI-realtime client (`litellm._arealtime`, experimental).

So: **net-viable only if Check 5 passes.** If the passthrough captures cost, the
rewrite + feature loss may be worth the ADR-0001/0002 alignment. If it doesn't,
the lighter alternative wins outright — keep the `google.genai` Vertex path but
**externalize `GCP_KEY`** and **emit a usage record to the gateway ingest
endpoint** at session end. That captures the two ADR-aligned wins (credentials out
of the pod, cost in the gateway) with none of the protocol risk.

Run Check 5 first, then Check 2 — they decide it.

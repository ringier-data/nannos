# Findings: voice agent → LiteLLM realtime

LiteLLM version/digest tested: `__________`
Date run: `__________`

## Verdicts

| # | Check | Verdict (GO / GATE / NO-GO) | Notes |
|---|---|---|---|
| 1 | Auth / region — Vertex AI + service account, `europe-west1` (confirm; doc says ✅) | | |
| 2 | Transcription — **output** arrives + **input** survives (input not in doc) | | |
| 3 | Protocol parity — audio I/O, voice, system prompt, text injection, 1 MCP tool call | | |
| 4 | Feature loss — scheduling hints / barge-in / context compression | | |
| 5 | Cost capture — realtime session yields a SpendLog row w/ attribution | | |

## Overall recommendation

> MIGRATE / DEFER / NO-GO — one paragraph, with the deciding check(s).

## Feature mapping (Gemini Live → OpenAI realtime)

| Gemini Live call | OpenAI-realtime equivalent | keep / remap / lost |
|---|---|---|
| `send_realtime_input(Blob 16kHz)` | | |
| `input/output_audio_transcription` | | |
| `send_tool_response(scheduling=…)` | | |
| `sc.interrupted` | | |
| `context_window_compression` | | |
| `send_client_content` (text injection) | | |
| prebuilt voice | | |

## If NO-GO: the lighter alternative

Did the "keep Vertex path, externalize `GCP_KEY` + emit usage to the gateway
ingest endpoint" option hold up? What would it take?

# LiteLLM Proxy pod as the Model Gateway

**Status:** accepted

We route all LLM calls through LiteLLM deployed as a standalone **proxy pod** (with its own Postgres), rather than the in-process LiteLLM SDK (`ChatLiteLLM`). Apps talk to it as an OpenAI-compatible endpoint via `ChatOpenAI(base_url=...)`.

## Why

The in-process SDK delivers the unified-interface benefit but **cannot register models at runtime** — adding a model would require an app redeploy. Runtime model management ("pick models from LiteLLM without redeploying") is a primary goal, and it depends on the proxy's DB-backed management API (`store_model_in_db`). The proxy also centralizes credentials (removing cloud creds from app pods), timeout/retry/fallback policy, and observability in one place.

## Consequences

- New stateful service to operate: the proxy + its Postgres. The proxy becomes a network hop and a potential SPOF — mitigated by replicas and a client-side fallback behind a feature flag (`LLM_GATEWAY_URL`).
- Model availability detection shifts from import-time credential probing to querying the proxy at runtime (new failure mode: proxy unavailable at boot).
- Through the OpenAI-compatible path, provider-native response fields collapse — see downstream decisions on cost keying and extended-thinking passthrough.

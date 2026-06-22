# Example Kubernetes deployment

Reference manifests for running the Nannos stack on Kubernetes (namespace `nannos`). They are
a starting point, not a turnkey install: replace every `YOUR_*` / `${...}` placeholder (image
tags, domains, Postgres host, OIDC, secrets) for your environment.

```
base/      Deployments + Services for each component (apps, clients, litellm-proxy)
overlay/   image patch, kustomization, and the Secret stubs you must fill in
```

## Model Gateway is the sole LLM path

Every LLM/embedding call goes through the LiteLLM proxy (`litellm-proxy.nannos.svc`); apps
never construct provider clients directly. Provider credentials (Azure/Bedrock/Vertex) live
**only** on the proxy (`litellm-proxy-secrets` + `litellm-proxy.yaml`) — the app pods hold no
provider keys, just their own gateway virtual key.

The four gateway-calling services — `orchestrator-agent`, `agent-runner`, `console` (backend),
and `catalog-worker` — set `LLM_GATEWAY_URL` and read `LLM_GATEWAY_API_KEY` from their secret.
`gateway_base_url()` raises if `LLM_GATEWAY_URL` is unset, so these pods cannot start serving
LLM traffic without it. (The clients and `voice-agent` make no gateway calls and have neither.)

### Virtual keys must be minted before the apps start

`LLM_GATEWAY_API_KEY` is a per-app **virtual key minted by the proxy** (for per-app cost
attribution) — not a static shared secret. The `YOUR_LLM_GATEWAY_VIRTUAL_KEY` placeholders in
`overlay/secrets.yaml` are filled in a two-phase bootstrap: deploy the proxy → mint one key per
app via `POST /key/generate` → deploy the apps with the minted keys.

See **Secrets & two-phase bootstrap** in [`packages/litellm-proxy/README.md`](../packages/litellm-proxy/README.md)
for the exact commands and ordering.

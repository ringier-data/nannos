# Nannos Model Gateway (LiteLLM proxy)

The single path for all LLM traffic in Nannos (ADR-0001). An OpenAI-compatible
proxy that owns provider routing, credentials, timeouts, observability, runtime
model registration, and proxy-side cost capture. Apps talk to it via
`LLM_GATEWAY_URL` and never construct provider clients directly.

## Contents (deployment-agnostic)
- `custom_logger.py` — `NannosCostLogger`: maps native usage (incl. cache/reasoning
  tokens) → Nannos billing units and POSTs to console-backend `/api/v1/usage/gateway-batch-log`
  with the attribution the app forwarded as `spend_logs_metadata` (ADR-0002).
- `Dockerfile` — `FROM ghcr.io/berriai/litellm:main-stable` (pin to the digest you
  ship; validated at **v1.89.2** in the spike) + the callback only.

The `config.yaml` (model_list, regions, `model_info` cost seeds, `store_model_in_db`)
is **deployment-specific and not committed here** — it's mounted at runtime:
- **k8s:** a ConfigMap in your deployment/GitOps repo, mounted at `/etc/litellm/config.yaml`.
- **local:** generated on the fly by `scripts/start-local.sh` (never committed).

## Runtime configuration (env)
- `LITELLM_MASTER_KEY` — proxy admin/master key.
- `DATABASE_URL` — Postgres connection for the Prisma store (model registry + SpendLogs); Prisma migrates on boot.
- Provider auth for whatever you route: AWS creds + `AWS_BEDROCK_REGION` (Bedrock), `AZURE_OPENAI_ENDPOINT`/`AZURE_OPENAI_API_KEY` (Azure), `GCP_PROJECT_ID`/`GCP_KEY` (Vertex).
- `CONSOLE_BACKEND_URL` + `GATEWAY_INGEST_TOKEN` — cost-ingestion target and its shared secret.

## Build
```sh
just build-pkg litellm-proxy            # local image
just push=true build-pkg litellm-proxy  # build + push
```

## Deployment
Run as a **private, in-cluster** service: inference and especially the management
routes (`/model/*`) must not be publicly exposed (ADR-0005) — reach them only from
within the cluster, with console-backend as the sole writer of `/model/*`. Provide
the env/secrets above through your platform's secret manager. Concrete manifests,
secret names, and credential sources are deployment-specific and live with your
deployment/GitOps configuration, not in this package.

## Secrets & two-phase bootstrap

Three net-new secrets (none are fetched from an existing store), plus the proxy's DB password:

| Secret | Origin |
|---|---|
| `LITELLM_MASTER_KEY` | you generate (`echo sk-$(openssl rand -hex 32)`); goes to the proxy and to console-backend |
| `GATEWAY_INGEST_TOKEN` | you generate (`openssl rand -hex 32`); **same value** on the proxy and console-backend |
| `LLM_GATEWAY_API_KEY` | **minted by the proxy** via `/key/generate`; one per LLM-calling app |
| `DATABASE_URL` password | the Postgres password for the proxy's DB user, from your secret manager |

App pods get **only** their virtual `LLM_GATEWAY_API_KEY` — never the master key or ingest token (ADR-0005).

Order matters because virtual keys are DB-minted:

1. **Deploy the proxy** with `LITELLM_MASTER_KEY`, `GATEWAY_INGEST_TOKEN`, provider creds and `DATABASE_URL` set (it migrates its schema and comes up).
2. **Mint an app key:**
   ```sh
   curl -s "$LLM_GATEWAY_URL/key/generate" \
     -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
     -H "Content-Type: application/json" -d '{"key_alias":"apps"}'
   # → {"key":"sk-..."}  → store as each app's LLM_GATEWAY_API_KEY
   ```
3. **Deploy the apps** with `LLM_GATEWAY_URL` + `LLM_GATEWAY_API_KEY` set.

See `docs/adr/0001..0005` and `spikes/litellm-proxy-verification/` for the design and validation harness.

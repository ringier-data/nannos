# Nannos Model Gateway (LiteLLM proxy)

The single path for all LLM traffic in Nannos. An OpenAI-compatible
proxy that owns provider routing, credentials, timeouts, observability, runtime
model registration, and proxy-side cost capture. Apps talk to it via
`LLM_GATEWAY_URL` and never construct provider clients directly.

## Contents (deployment-agnostic)
- `custom_logger.py` — `NannosCostLogger`: maps native usage (incl. cache/reasoning
  tokens) → Nannos billing units and POSTs to console-backend `/api/v1/usage/gateway-batch-log`
  with the attribution the app forwarded as `spend_logs_metadata`.
- `Dockerfile` — `FROM ghcr.io/berriai/litellm` + the callback only.

The `config.yaml` (model_list, regions, `model_info` cost seeds, `store_model_in_db`)
is **deployment-specific and not committed here** — it's mounted at runtime:
- **k8s:** a ConfigMap in your deployment/GitOps repo, mounted at `/etc/litellm/config.yaml`.
- **local:** generated on the fly by `scripts/start-local.sh` (never committed).

## Runtime configuration (env)
- `LITELLM_MASTER_KEY` — proxy admin/master key.
- `DATABASE_URL` — Postgres connection for the Prisma store (model registry + SpendLogs); Prisma migrates on boot.
- Provider auth for whatever you route: AWS creds + `AWS_BEDROCK_REGION` (Bedrock), `AZURE_API_BASE`/`AZURE_OPENAI_API_KEY` (Azure OpenAI, `azure/*`), `AZURE_AI_API_BASE`/`AZURE_AI_API_KEY` (Azure AI Studio, `azure_ai/*`), `GCP_PROJECT_ID`/`GCP_KEY` (Vertex).
- `CONSOLE_BACKEND_URL` + `GATEWAY_INGEST_TOKEN` — cost-ingestion target and its shared secret.

**Vertex auth is ADC, and the proxy is the authority.** Vertex credentials are resolved
via `google.auth.default()` from `GOOGLE_APPLICATION_CREDENTIALS` (the `GCP_KEY` JSON written to a
file): k8s projects the secret to `/secrets/gcp/sa.json`, and `scripts/start-local.sh` mounts the
same file locally — so dev and prod authenticate identically. Consequently **model registrations
must carry no per-model `vertex_credentials`** (console-backend sends none). Runtime-registered
(DB) models do **not** resolve `os.environ/*` refs — the proxy config is settings-only with no
`model_list` — so an injected `vertex_credentials: os.environ/GCP_KEY` reaches `json.loads()` and
fails with "Unable to load vertex credentials … JSONDecodeError". Only config-defined `model_list`
entries resolve `os.environ/GCP_KEY`; DB models rely solely on ADC. (Pointing ADC at a real file
also avoids the GCE-metadata-probe startup hang you get when only `GCP_KEY` is set.)

## Build
```sh
just build-pkg litellm-proxy            # local image
just push=true build-pkg litellm-proxy  # build + push
```

## Deployment
Run as a **private, in-cluster** service: inference and especially the management
routes (`/model/*`) must not be publicly exposed — reach them only from
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

App pods get **only** their virtual `LLM_GATEWAY_API_KEY` — never the master key or ingest token.

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

## Bring-your-own LiteLLM gateway

If you already operate a LiteLLM proxy you can skip provisioning this pod entirely
(toggle it off in your GitOps repo) and route Nannos at your existing endpoint via
`LLM_GATEWAY_URL`. Cost capture still works — `custom_logger.py` is a plain LiteLLM
`CustomLogger` with no dependency on this pod (only `litellm` + `httpx` + stdlib), so
you carry **four things** into your proxy:

1. **The callback module** — put `custom_logger.py` on the proxy's `PYTHONPATH`. LiteLLM
   resolves a `module.attr` callback **relative to the config dir**, so it must sit next
   to your mounted config (this image uses `/etc/litellm` + `PYTHONPATH=/etc/litellm`).
   Mount it as a ConfigMap/volume, or bake it into your own image layer.
2. **The config line** — `litellm_settings: { callbacks: custom_logger.proxy_handler_instance }`.
3. **Two env vars** — `CONSOLE_BACKEND_URL` and `GATEWAY_INGEST_TOKEN` (the latter must
   equal console-backend's `GATEWAY_INGEST_TOKEN`).
4. **A network path** — your proxy must be able to POST to console-backend
   `/api/v1/usage/gateway-batch-log`.

Two correctness contracts must hold regardless of which proxy serves traffic:

- **Alias names = rate-card patterns.** The logger bills on `metadata.model_group` (the
  public alias the caller requested, not the resolved deployment id), and console-backend
  matches Rate Cards against that name. If your `model_list` exposes different `model_name`s,
  rate cards miss and calls bill at **$0**. Keep aliases aligned with the Nannos rate cards.
- **Attribution passthrough — no app change.** Apps forward `user_sub`/`conversation_id`/etc.
  as `spend_logs_metadata` in request metadata; the logger reads it from
  `litellm_params.metadata.spend_logs_metadata`. Upstream LiteLLM preserves caller metadata,
  so this works on any LiteLLM proxy. Records with no `user_sub` are skipped (can't be billed).

**Caveat — LiteLLM version.** `_billing_unit_breakdown` parses LiteLLM's normalized usage
shapes (cache-inclusive vs additive token accounting), which is why the image is pinned to an
exact version (**v1.90.0-rc.1** — bumped from v1.89.2 for the Vertex multi-region context-caching
fix, BerriAI/litellm#29573; see the Dockerfile). On a materially different version the
cache/reasoning bucketing can drift → mis-billing, so this bump MUST be validated: run a known
call (ideally one that exercises cache-read/cache-creation) and confirm the billing-unit
breakdown still matches before promoting past dev. Match this version or re-validate.

**Escape hatch.** If touching your proxy image is off the table, skip the in-process callback
and instead consume LiteLLM's native spend logs (DB / generic webhook), transforming them into
the `/api/v1/usage/gateway-batch-log` payload out-of-band. This decouples you from the version
pin but you must re-derive the billing-unit breakdown (base / cache-creation / cache-read /
reasoning) yourself — which is most of what `custom_logger.py` does.

See the **Model Gateway** section of the repo-root `AGENTS.md` for the design rationale.

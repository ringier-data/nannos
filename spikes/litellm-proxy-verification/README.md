# Spike: LiteLLM Gateway verification

**Throwaway.** This is not production code and must not be merged into a service. It exists to prove (or break) four claims from the model-gateway grill before migration work starts. See `../../CONTEXT.md` and `../../docs/adr/0001..0005`.

The four checks, and the ADR each de-risks:

| Check | Verifies | ADR | Needs |
|---|---|---|---|
| 1 — thinking | `reasoning_effort` reaches Bedrock Claude 4.6; quantify "medium ≠ 10k" | 0003 | real Bedrock |
| 2 — timeouts (**gate**) | first-token *and* inter-chunk timeouts fire on Bedrock streaming | 0004 | mock + real Bedrock |
| 3 — cost fidelity | proxy `CustomLogger` + SpendLogs keep `cache_creation`/`cache_read`/`reasoning` + real provider/model | 0002 | real Bedrock + Postgres |
| 4 — attribution | ContextVar→header hook carries all fields; correct under concurrency | 0002 | mock is enough |

## ⚠️ Pin the LiteLLM version

`docker-compose.yml` uses `ghcr.io/berriai/litellm:main-stable`. **Replace it with the exact version/digest you test and record it here** — Check 2's result is version-specific (bugs [#23375](https://github.com/BerriAI/litellm/issues/23375), [#19909](https://github.com/BerriAI/litellm/issues/19909)).

- Tested LiteLLM version/digest: `__________` (fill in)

## Prerequisites

- Docker + Docker Compose
- `uv`
- AWS credentials with **Bedrock Claude enabled** in your region. Export before `docker compose up`:
  ```sh
  export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_SESSION_TOKEN=...   # or AWS_PROFILE
  export AWS_BEDROCK_REGION=eu-west-1   # region where claude-sonnet-4.6 is enabled
  ```

## Run

```sh
cd spikes/litellm-proxy-verification
export AWS_BEDROCK_REGION=eu-west-1   # + AWS creds as above
mkdir -p captured && : > captured/events.jsonl
docker compose up -d            # proxy (:4000) + postgres + mock upstream
docker compose logs -f litellm  # wait for "Application startup complete" + prisma migrate

# run all checks (reuses agent-common's venv for langchain-openai/httpx/openai)
uv run --project ../../packages/agent-common \
  pytest . -v
```

Bedrock-dependent tests are marked `@pytest.mark.integration` and run by default here. To run only the no-Bedrock checks: `pytest . -v -m "not integration"`.

## Interpreting results → fill `SPIKE-FINDINGS.md`

- Each check → GO / GATE-TRIGGERED / NEEDS-CONFIG.
- Check 1: record resolved reasoning-token usage per effort level.
- Check 2 (**the gate**): if the proxy does not enforce first-token AND inter-chunk timeouts on Bedrock streaming, ADR-0004's escalation to a **client-side inter-chunk watchdog (3-C)** becomes mandatory before go-live.
- Feed verdicts back into ADR-0002/0003/0004 and the spike checklist in `CONTEXT.md`.

## Teardown

```sh
docker compose down -v
```

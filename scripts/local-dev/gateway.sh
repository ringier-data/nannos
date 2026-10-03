#!/usr/bin/env bash
set -euo pipefail

# Service `gateway` of process-compose.yaml: the stack's Model Gateway (LiteLLM proxy) as a
# foreground container, so process-compose owns it (logs, readiness, stop). Its config and GCP
# service-account file are assembled by start-local.sh, which has the secrets.
#
# Gateway-only architecture: all LLM calls route through the proxy, there is no per-provider
# fallback. The services reach it on localhost:$LLM_GATEWAY_PORT.

ROOT="$NANNOS_ROOT"
CONTAINER="$NANNOS_GW_CONTAINER"

# A container left by a killed stack would hold the name (and the port).
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true

# Temporary AWS credentials of the profile, for Bedrock: the SDK's profile/SSO chain is not
# visible inside the container. Handed to the gateway only — the services use the profile itself.
aws_env=()
if [[ -n "${NANNOS_GW_AWS_ACCESS_KEY_ID:-}" ]]; then
  aws_env=(-e "AWS_ACCESS_KEY_ID=$NANNOS_GW_AWS_ACCESS_KEY_ID"
           -e "AWS_SECRET_ACCESS_KEY=$NANNOS_GW_AWS_SECRET_ACCESS_KEY"
           -e "AWS_SESSION_TOKEN=${NANNOS_GW_AWS_SESSION_TOKEN:-}")
fi

# Pod-level Vertex auth via ADC, mirroring deployment (k8s projects GCP_KEY to a file and points
# GOOGLE_APPLICATION_CREDENTIALS at it), so google.auth.default() resolves Vertex creds for BOTH
# config-defined and runtime-registered (DB) models — the latter do NOT resolve os.environ/GCP_KEY,
# exactly as in deployment, which is why console registrations must carry no vertex_credentials.
# A real file also avoids the GCE-metadata-probe hang you get when only GCP_KEY is set. (GCP_KEY
# stays for config-defined model_list entries that still reference os.environ/GCP_KEY.)
gcp_env=()
if [[ -n "${NANNOS_GW_GCP_SA:-}" ]]; then
  gcp_env=(-v "$NANNOS_GW_GCP_SA:/secrets/gcp/sa.json:ro" -e GOOGLE_APPLICATION_CREDENTIALS=/secrets/gcp/sa.json)
fi

# exec: process-compose's stop signal reaches `docker run`, which passes it to the container;
# --rm removes it. (process-compose.yaml also runs `docker stop` as the shutdown command.)
exec docker run --rm --name "$CONTAINER" \
  -p "${LLM_GATEWAY_PORT}:4000" \
  --add-host=host.docker.internal:host-gateway \
  -v "$NANNOS_GW_CONFIG:/etc/litellm/config.yaml:ro" \
  -v "$ROOT/packages/litellm-proxy/custom_logger.py:/etc/litellm/custom_logger.py:ro" \
  -v "$ROOT/packages/ringier-a2a-sdk/ringier_a2a_sdk/model_capabilities.py:/etc/litellm/nannos_model_capabilities.py:ro" \
  -e PYTHONPATH=/etc/litellm \
  -e LITELLM_MASTER_KEY="$LLM_GATEWAY_API_KEY" \
  -e LITELLM_DATABASE_URL="$LITELLM_DATABASE_URL" \
  -e UI_USERNAME="${LITELLM_UI_USERNAME:-admin}" \
  -e UI_PASSWORD="${LITELLM_UI_PASSWORD:-sk-nannos-local}" \
  -e AWS_BEDROCK_REGION="${AWS_BEDROCK_REGION:-eu-central-1}" \
  -e AWS_REGION="${AWS_BEDROCK_REGION:-eu-central-1}" \
  ${aws_env[@]+"${aws_env[@]}"} \
  -e AZURE_API_BASE="${AZURE_API_BASE:-}" \
  -e AZURE_OPENAI_API_KEY="${AZURE_OPENAI_API_KEY:-}" \
  -e AZURE_AI_API_BASE="${AZURE_AI_API_BASE:-}" \
  -e AZURE_AI_API_KEY="${AZURE_AI_API_KEY:-}" \
  -e GCP_PROJECT_ID="${GCP_PROJECT_ID:-}" \
  -e GCP_KEY="${GCP_KEY:-}" \
  ${gcp_env[@]+"${gcp_env[@]}"} \
  -e DEFAULT_VERTEXAI_LOCATION="${DEFAULT_VERTEXAI_LOCATION:-eu}" \
  -e CONSOLE_BACKEND_URL="http://host.docker.internal:${CONSOLE_BACKEND_PORT}" \
  -e GATEWAY_INGEST_TOKEN="$GATEWAY_INGEST_TOKEN" \
  "$NANNOS_LITELLM_IMAGE" --config /etc/litellm/config.yaml --port 4000 </dev/null

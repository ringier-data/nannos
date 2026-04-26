#!/usr/bin/env bash
set -euo pipefail

# ─── Nannos Local Development Startup ──────────────────────────────
#
# Starts all services from a clean clone.
# Prerequisites: docker, uv, node/npm, tmux
#
# Usage:
#   OPENAI_COMPATIBLE_BASE_URL=http://localhost:1234 ./scripts/start-local.sh
#
# OR, to use cloud LLMs (Azure OpenAI, Bedrock, GCP Vertex) via AWS:
#   AWS_PROFILE=your-profile ./scripts/start-local.sh
#
# Both can be combined to enable local + cloud models simultaneously.
#
# Flags:
#   --debug   Start Python services with debugpy for VS Code debugging
#             (ports: backend=5678, orchestrator=5679, creator=5680,
#              runner=5682, voice-agent=5683)
#
# The base URL should point to the root of your LLM server — /v1 is appended
# automatically if absent (works with LM Studio, Ollama, vLLM, etc.).
#
# You can also place env vars in a .env file at the repo root:
#   echo 'OPENAI_COMPATIBLE_BASE_URL=http://localhost:1234' > .env
#   ./scripts/start-local.sh
#
# Optional env vars:
#   OPENAI_COMPATIBLE_MODEL  - model name as listed by GET /v1/models (required
#                              for LM Studio; defaults to "default" otherwise)
#   AWS_PROFILE              - AWS profile for SSM secrets + Bedrock/S3/DynamoDB
#   OIDC_ISSUER              - External OIDC issuer URL (skips local Keycloak)
#   MCP_GATEWAY_URL          - MCP gateway URL (optional, tools disabled if unset)
#   MCP_GATEWAY_CLIENT_ID    - MCP gateway client ID (defaults to "gatana")
#
# Cloud LLM providers (fetched from SSM when AWS_PROFILE is set):
#   AZURE_OPENAI_API_KEY     - Azure OpenAI API key
#   AZURE_OPENAI_ENDPOINT    - Azure OpenAI endpoint URL
#   GCP_KEY                  - GCP service account key JSON (Vertex AI / Gemini)
#   GCP_PROJECT_ID           - GCP project ID (defaults to "rcplus-alloy-gcp")
#   GCP_LOCATION             - GCP region (defaults to "global")
#
# Tracing (fetched from SSM when AWS_PROFILE is set):
#   LANGSMITH_API_KEY        - LangSmith tracing API key
#   LANGSMITH_TRACING        - Enable tracing ("true"/"false", defaults to "false")
#   LANGSMITH_ENDPOINT       - LangSmith API endpoint
#   LANGSMITH_PROJECT        - LangSmith project name
#
# Catalog & Google Drive sync (fetched from SSM when AWS_PROFILE is set):
#   CATALOG_VECTOR_BUCKET_NAME      - S3 bucket for catalog vector storage
#   CATALOG_THUMBNAILS_S3_BUCKET    - S3 bucket for catalog thumbnails
#   GOOGLE_OAUTH_CLIENT_ID          - Google OAuth client ID for Drive sync
#   GOOGLE_OAUTH_CLIENT_SECRET      - Google OAuth client secret for Drive sync
#
# Twilio (fetched from SSM when AWS_PROFILE is set):
#   TWILIO_ACCOUNT_SID        - Twilio Account SID (voice agent + phone verification)
#   TWILIO_API_KEY             - Twilio API Key (voice agent)
#   TWILIO_API_SECRET           - Twilio API Secret (voice agent)
#   TWILIO_VERIFY_SERVICE_SID  - Twilio Verify Service SID (phone verification)
#   TWILIO_VERIFY_API_KEY      - Twilio Verify API Key (phone verification)
#   TWILIO_VERIFY_API_SECRET   - Twilio Verify API Secret (phone verification)
# ───────────────────────────────────────────────────────────────────

# ─── 0. Parse flags ────────────────────────────────────────────────

_DEBUG_MODE=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --debug) _DEBUG_MODE=1; shift ;;
    *) echo "Unknown flag: $1"; exit 1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOCAL_DEV_DIR="$SCRIPT_DIR/local-dev"

# Source .env from repo root if present (does not override existing env vars)
_DOTENV_LOADED=false
if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  source "$ROOT_DIR/.env"
  set +a
  _DOTENV_LOADED=true
fi

CYAN='\033[1;36m'
GREEN='\033[1;32m'
RED='\033[1;31m'
YELLOW='\033[1;33m'
DIM='\033[2m'
RESET='\033[0m'

log()  { printf "${CYAN}▸ %s${RESET}\n" "$*"; }
ok()   { printf "${GREEN}✓ %s${RESET}\n" "$*"; }
warn() { printf "${YELLOW}⚠ %s${RESET}\n" "$*"; }
err()  { printf "${RED}✗ %s${RESET}\n" "$*"; exit 1; }

# ─── 1. Check prerequisites ───────────────────────────────────────

log "Checking prerequisites..."

missing=()
command -v docker  >/dev/null 2>&1 || missing+=(docker)
command -v uv      >/dev/null 2>&1 || missing+=(uv)
command -v node    >/dev/null 2>&1 || missing+=(node)
command -v npm     >/dev/null 2>&1 || missing+=(npm)

if [[ ${#missing[@]} -gt 0 ]]; then
  err "Missing required tools: ${missing[*]}
  Install them:
    brew install docker uv node tmux"
fi

docker info >/dev/null 2>&1 || err "Docker daemon is not running"

ok "All prerequisites found"

# ─── 2. Detect scenario ───────────────────────────────────────────

_HAS_LOCAL_LLM=false
_HAS_AWS=false
_HAS_REMOTE_OIDC=false
_HAS_MCP=false

[[ -n "${OPENAI_COMPATIBLE_BASE_URL:-}" ]] && _HAS_LOCAL_LLM=true
[[ -n "${AWS_PROFILE:-}" ]]               && _HAS_AWS=true
[[ -n "${OIDC_ISSUER:-}" ]]               && _HAS_REMOTE_OIDC=true
[[ -n "${MCP_GATEWAY_URL:-}" ]]           && _HAS_MCP=true

# Must have at least one LLM source
if [[ "$_HAS_LOCAL_LLM" == false && "$_HAS_AWS" == false ]]; then
  printf "\n"
  printf "${RED}No LLM source configured.${RESET} Set at least one of:\n"
  printf "\n"
  printf "  ${GREEN}1) Full Local${RESET} — local LLM only, local Keycloak\n"
  printf "     ${DIM}OPENAI_COMPATIBLE_BASE_URL=http://localhost:1234 $0${RESET}\n"
  printf "\n"
  printf "  ${GREEN}2) Local + AWS${RESET} — local LLM + cloud models (Bedrock, Azure, GCP), local Keycloak\n"
  printf "     ${DIM}AWS_PROFILE=my-profile OPENAI_COMPATIBLE_BASE_URL=http://localhost:1234 $0${RESET}\n"
  printf "\n"
  printf "  ${GREEN}3) Local + AWS + Remote OIDC${RESET} — cloud models + production Keycloak\n"
  printf "     ${DIM}AWS_PROFILE=my-profile OIDC_ISSUER=https://login.p.nannos.rcplus.io/realms/nannos $0${RESET}\n"
  printf "\n"
  printf "  All scenarios optionally accept: ${DIM}MCP_GATEWAY_URL=...${RESET}\n"
  printf "\n"
  printf "  ${YELLOW}Tip: Place env vars in .env at the repo root instead of the command line.${RESET}\n"
  printf "\n"
  exit 1
fi

# Remote OIDC without AWS requires manual secret entry
if [[ "$_HAS_REMOTE_OIDC" == true && "$_HAS_AWS" == false ]]; then
  _OIDC_MODE="remote-manual"
elif [[ "$_HAS_REMOTE_OIDC" == true && "$_HAS_AWS" == true ]]; then
  _OIDC_MODE="remote-ssm"
else
  _OIDC_MODE="local"
fi

# ─── 2b. Present plan and confirm ────────────────────────────────

printf "\n"
printf "${CYAN}┌────────────────────────────────────────────────────────┐${RESET}\n"
printf "${CYAN}│  Nannos Local Dev — Startup Plan                       │${RESET}\n"
printf "${CYAN}├────────────────────────────────────────────────────────┤${RESET}\n"

# Scenario label
if [[ "$_OIDC_MODE" == "remote-ssm" ]]; then
  printf "${CYAN}│${RESET}  Scenario:  ${GREEN}Local Apps + AWS + Remote OIDC${RESET}             ${CYAN}│${RESET}\n"
elif [[ "$_OIDC_MODE" == "remote-manual" ]]; then
  printf "${CYAN}│${RESET}  Scenario:  ${GREEN}Local Apps + Remote OIDC (manual secret)${RESET}   ${CYAN}│${RESET}\n"
elif [[ "$_HAS_AWS" == true ]]; then
  printf "${CYAN}│${RESET}  Scenario:  ${GREEN}Local Apps + AWS${RESET}                           ${CYAN}│${RESET}\n"
else
  printf "${CYAN}│${RESET}  Scenario:  ${GREEN}Full Local${RESET}                                 ${CYAN}│${RESET}\n"
fi

printf "${CYAN}├────────────────────────────────────────────────────────┤${RESET}\n"

# LLM
printf "${CYAN}│${RESET}  LLM models:                                           ${CYAN}│${RESET}\n"
if [[ "$_HAS_LOCAL_LLM" == true ]]; then
  printf "${CYAN}│${RESET}    ${GREEN}✓${RESET} Local LLM  ${DIM}($OPENAI_COMPATIBLE_BASE_URL)${RESET}\n"
else
  printf "${CYAN}│${RESET}    ${DIM}✗ Local LLM  (set OPENAI_COMPATIBLE_BASE_URL)${RESET}\n"
fi
if [[ "$_HAS_AWS" == true ]]; then
  printf "${CYAN}│${RESET}    ${GREEN}✓${RESET} AWS Bedrock     ${DIM}(via profile: $AWS_PROFILE)${RESET}\n"
  printf "${CYAN}│${RESET}    ${GREEN}✓${RESET} Azure OpenAI    ${DIM}(key from SSM)${RESET}                    ${CYAN}│${RESET}\n"
  printf "${CYAN}│${RESET}    ${GREEN}✓${RESET} GCP Vertex AI   ${DIM}(key from SSM)${RESET}                    ${CYAN}│${RESET}\n"
else
  printf "${CYAN}│${RESET}    ${DIM}✗ Cloud models   (set AWS_PROFILE to enable)${RESET}\n"
fi

printf "${CYAN}│${RESET}                                                        ${CYAN}│${RESET}\n"

# Authentication
printf "${CYAN}│${RESET}  Authentication:                                       ${CYAN}│${RESET}\n"
if [[ "$_OIDC_MODE" == "local" ]]; then
  printf "${CYAN}│${RESET}    ${GREEN}✓${RESET} Local Keycloak  ${DIM}(localhost:8180, auto-configured)${RESET}\n"
  printf "${CYAN}│${RESET}    ${DIM}  Login: test@local.dev / password${RESET}\n"
elif [[ "$_OIDC_MODE" == "remote-ssm" ]]; then
  printf "${CYAN}│${RESET}    ${GREEN}✓${RESET} Remote OIDC      ${DIM}($OIDC_ISSUER)${RESET}\n"
  printf "${CYAN}│${RESET}    ${GREEN}✓${RESET} Secrets from SSM ${DIM}(per-service client secrets)${RESET}\n"
elif [[ "$_OIDC_MODE" == "remote-manual" ]]; then
  printf "${CYAN}│${RESET}    ${GREEN}✓${RESET} Remote OIDC     ${DIM}($OIDC_ISSUER)${RESET}\n"
  printf "${CYAN}│${RESET}    ${YELLOW}⚠${RESET} Manual secret   ${DIM}(you will be prompted)${RESET}\n"
fi

printf "${CYAN}│${RESET}                                                        ${CYAN}│${RESET}\n"

# Infrastructure
printf "${CYAN}│${RESET}  Infrastructure:                                       ${CYAN}│${RESET}\n"
printf "${CYAN}│${RESET}    ${GREEN}✓${RESET} PostgreSQL (console)   ${DIM}(Docker, localhost:5401)${RESET}\n"
printf "${CYAN}│${RESET}    ${GREEN}✓${RESET} PostgreSQL (docstore)  ${DIM}(Docker, localhost:5402)${RESET}\n"
if [[ "$_OIDC_MODE" == "local" ]]; then
  printf "${CYAN}│${RESET}    ${GREEN}✓${RESET} Keycloak        ${DIM}(Docker, localhost:8180)${RESET}\n"
else
  printf "${CYAN}│${RESET}    ${DIM}✗ Keycloak        (skipped — using remote OIDC)${RESET}\n"
fi
printf "${CYAN}│${RESET}    ${GREEN}✓${RESET} DB migrations   ${DIM}(Rambler, auto-applied)${RESET}\n"
printf "${CYAN}│${RESET}    ${GREEN}✓${RESET} Slack (FE+BE)   ${DIM}(Docker)${RESET}\n"

printf "${CYAN}│${RESET}                                                        ${CYAN}│${RESET}\n"

# Optional
# Debugging
if [[ -n "$_DEBUG_MODE" ]]; then
  printf "${CYAN}│${RESET}  Debugging:                                            ${CYAN}│${RESET}\n"
  printf "${CYAN}│${RESET}    ${GREEN}✓${RESET} debugpy enabled ${DIM}(attach via VS Code launch.json)${RESET}\n"
  printf "${CYAN}│${RESET}    ${DIM}  backend=5678 orchestrator=5679 creator=5680${RESET}\n"
  printf "${CYAN}│${RESET}    ${DIM}  runner=5682 voice-agent=5683${RESET}\n"
  printf "${CYAN}│${RESET}                                                        ${CYAN}│${RESET}\n"
fi

printf "${CYAN}│${RESET}  Optional:                                             ${CYAN}│${RESET}\n"
if [[ "$_HAS_MCP" == true ]]; then
  printf "${CYAN}│${RESET}    ${GREEN}✓${RESET} MCP Gateway     ${DIM}($MCP_GATEWAY_URL)${RESET}\n"
else
  printf "${CYAN}│${RESET}    ${DIM}✗ MCP Gateway     (set MCP_GATEWAY_URL to enable)${RESET}\n"
fi
if [[ -n "${LANGSMITH_API_KEY:-}" ]]; then
  printf "${CYAN}│${RESET}    ${GREEN}✓${RESET} LangSmith       ${DIM}(key set)${RESET}\n"
elif [[ "$_HAS_AWS" == true ]]; then
  printf "${CYAN}│${RESET}    ${GREEN}✓${RESET} LangSmith       ${DIM}(key from SSM)${RESET}\n"
else
  printf "${CYAN}│${RESET}    ${DIM}✗ LangSmith       (set LANGSMITH_API_KEY or AWS_PROFILE)${RESET}\n"
fi

printf "${CYAN}│${RESET}                                                        ${CYAN}│${RESET}\n"

# AWS actions warning
if [[ "$_HAS_AWS" == true ]]; then
  printf "${CYAN}│${RESET}  ${YELLOW}Will fetch secrets from AWS SSM (profile: $AWS_PROFILE)${RESET}\n"
fi

# .env file status
if [[ "$_DOTENV_LOADED" == true ]]; then
  printf "${CYAN}│${RESET}                                                        ${CYAN}│${RESET}\n"
  printf "${CYAN}│${RESET}  ${DIM}Config loaded from: .env${RESET}\n"
else
  printf "${CYAN}│${RESET}                                                        ${CYAN}│${RESET}\n"
  printf "${CYAN}│${RESET}  ${YELLOW}Tip: create .env at repo root to avoid typing env vars${RESET}\n"
fi

printf "${CYAN}└────────────────────────────────────────────────────────┘${RESET}\n"
printf "\n"

# Confirm
printf "${CYAN}▸ Proceed? [Y/n] ${RESET}"
read -r _confirm
if [[ "${_confirm:-Y}" =~ ^[Nn] ]]; then
  log "Aborted. Set env vars and re-run (or put them in .env at the repo root):"
  printf "\n"
  printf "  ${DIM}# Full Local${RESET}\n"
  printf "  ${DIM}OPENAI_COMPATIBLE_BASE_URL=http://localhost:1234 $0${RESET}\n"
  printf "\n"
  printf "  ${DIM}# Local + AWS${RESET}\n"
  printf "  ${DIM}AWS_PROFILE=my-profile OPENAI_COMPATIBLE_BASE_URL=http://localhost:1234 $0${RESET}\n"
  printf "\n"
  printf "  ${DIM}# Local + AWS + Remote OIDC${RESET}\n"
  printf "  ${DIM}AWS_PROFILE=my-profile OIDC_ISSUER=https://login.p.nannos.rcplus.io/realms/nannos $0${RESET}\n"
  printf "\n"
  printf "  ${DIM}# Or create .env at repo root:${RESET}\n"
  printf "  ${DIM}echo 'OPENAI_COMPATIBLE_BASE_URL=http://localhost:1234' > .env${RESET}\n"
  printf "  ${DIM}echo 'AWS_PROFILE=my-profile' >> .env${RESET}\n"
  printf "\n"
  exit 0
fi

printf "\n"

# ─── 3. Configure environment ─────────────────────────────────────

# ── Local LLM discovery ──
if [[ "$_HAS_LOCAL_LLM" == true ]]; then
  log "Discovering local LLM models..."

  _LLM_BASE="${OPENAI_COMPATIBLE_BASE_URL%/}"
  [[ "$_LLM_BASE" == */v1 ]] || _LLM_BASE="${_LLM_BASE}/v1"

  _MODELS_JSON=$(curl -sf "${_LLM_BASE}/models" 2>/dev/null || true)
  if [[ -n "$_MODELS_JSON" ]]; then
    _MODEL_IDS=$(echo "$_MODELS_JSON" | python3 -c "
import sys, json
data = json.load(sys.stdin)
ids = [m['id'] for m in data.get('data', [])]
print('\n'.join(ids))
" 2>/dev/null || true)

    if [[ -n "$_MODEL_IDS" ]]; then
      ok "Available models on LLM server:"
      while IFS= read -r _m; do
        printf "    ${DIM}• %s${RESET}\n" "$_m"
      done <<< "$_MODEL_IDS"

      if [[ -z "${OPENAI_COMPATIBLE_MODEL:-}" ]]; then
        OPENAI_COMPATIBLE_MODEL=$(echo "$_MODEL_IDS" | head -1)
        ok "Auto-selected model: $OPENAI_COMPATIBLE_MODEL"
      else
        log "Using specified model: $OPENAI_COMPATIBLE_MODEL"
      fi
    else
      warn "Could not parse model list from LLM server"
    fi
  else
    warn "Could not reach ${_LLM_BASE}/models — is the LLM server running?"
    if [[ -z "${OPENAI_COMPATIBLE_MODEL:-}" ]]; then
      warn "OPENAI_COMPATIBLE_MODEL not set; using 'default'"
      OPENAI_COMPATIBLE_MODEL="default"
    fi
  fi
fi

# ── AWS secrets & cloud providers ──
AZURE_OPENAI_API_KEY="${AZURE_OPENAI_API_KEY:-}"
AZURE_OPENAI_ENDPOINT="${AZURE_OPENAI_ENDPOINT:-}"
AWS_BEDROCK_REGION="${AWS_BEDROCK_REGION:-}"
GCP_KEY="${GCP_KEY:-}"
GCP_PROJECT_ID="${GCP_PROJECT_ID:-}"
GCP_LOCATION="${GCP_LOCATION:-}"
CHECKPOINT_DYNAMODB_TABLE_NAME="${CHECKPOINT_DYNAMODB_TABLE_NAME:-}"
CHECKPOINT_S3_BUCKET_NAME="${CHECKPOINT_S3_BUCKET_NAME:-}"
CHECKPOINT_AWS_REGION="${CHECKPOINT_AWS_REGION:-}"
DOCUMENT_STORE_S3_BUCKET="${DOCUMENT_STORE_S3_BUCKET:-}"
FILES_S3_BUCKET="${FILES_S3_BUCKET:-}"
CATALOG_VECTOR_BUCKET_NAME="${CATALOG_VECTOR_BUCKET_NAME:-}"
CATALOG_THUMBNAILS_S3_BUCKET="${CATALOG_THUMBNAILS_S3_BUCKET:-}"
GOOGLE_OAUTH_CLIENT_ID="${GOOGLE_OAUTH_CLIENT_ID:-}"
GOOGLE_OAUTH_CLIENT_SECRET="${GOOGLE_OAUTH_CLIENT_SECRET:-}"
TWILIO_ACCOUNT_SID="${TWILIO_ACCOUNT_SID:-}"
TWILIO_API_KEY="${TWILIO_API_KEY:-}"
TWILIO_API_SECRET="${TWILIO_API_SECRET:-}"
TWILIO_VERIFY_SERVICE_SID="${TWILIO_VERIFY_SERVICE_SID:-}"
TWILIO_VERIFY_API_KEY="${TWILIO_VERIFY_API_KEY:-}"
TWILIO_VERIFY_API_SECRET="${TWILIO_VERIFY_API_SECRET:-}"

if [[ "$_HAS_AWS" == true ]]; then
  log "Fetching secrets from AWS SSM (profile: $AWS_PROFILE)..."

  if AZURE_OPENAI_API_KEY=$(aws ssm get-parameter --name /nannos/openai-api-key-chatgpt-4o --output json --with-decryption 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['Parameter']['Value'])" 2>/dev/null); then
    AZURE_OPENAI_ENDPOINT="https://rcplus-alloy-eu-prod.openai.azure.com"
    ok "Azure OpenAI configured"
  else
    warn "Could not fetch AZURE_OPENAI_API_KEY from SSM — Azure OpenAI disabled"
    AZURE_OPENAI_API_KEY=""
  fi

  if _GCP_KEY=$(aws ssm get-parameter --name /nannos/infrastructure-agents/gcp-key --output json --with-decryption 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['Parameter']['Value'])" 2>/dev/null); then
    GCP_KEY="$_GCP_KEY"
    GCP_PROJECT_ID="rcplus-alloy-gcp"
    GCP_LOCATION="global"
    ok "GCP Vertex AI configured"
  else
    warn "Could not fetch GCP_KEY from SSM — Vertex AI disabled"
  fi

  if [[ -z "${LANGSMITH_API_KEY:-}" ]]; then
    if _LS_KEY=$(aws ssm get-parameter --name /nannos/infrastructure-agents/langsmith-api-key --output json --with-decryption 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['Parameter']['Value'])" 2>/dev/null); then
      LANGSMITH_API_KEY="$_LS_KEY"
      LANGSMITH_TRACING="true"
      LANGSMITH_ENDPOINT="https://eu.api.smith.langchain.com"
      LANGSMITH_PROJECT="dev-nannos-agent-framework"
      ok "LangSmith tracing configured from SSM"
    else
      warn "Could not fetch LANGSMITH_API_KEY from SSM"
    fi
  fi

  AWS_BEDROCK_REGION="eu-central-1"
  ok "AWS Bedrock enabled (region: $AWS_BEDROCK_REGION)"

  CHECKPOINT_DYNAMODB_TABLE_NAME="dev-nannos-infrastructure-agents-langgraph-checkpoints"
  CHECKPOINT_S3_BUCKET_NAME="dev-nannos-infrastructure-agents-orchestrator-checkpoints"
  CHECKPOINT_AWS_REGION="eu-central-1"
  DOCUMENT_STORE_S3_BUCKET="dev-nannos-infrastructure-agents-files"
  FILES_S3_BUCKET="dev-nannos-infrastructure-agents-files"
  CATALOG_VECTOR_BUCKET_NAME="dev-nannos-infrastructure-agents-catalog-vectors"
  CATALOG_THUMBNAILS_S3_BUCKET="dev-nannos-infrastructure-agents-catalog-thumbnails"

  # Google OAuth for catalog Drive sync (optional)
  if _GOAUTH_ID=$(aws ssm get-parameter --name /nannos/infrastructure-agents/google-oauth-client-id --output json --with-decryption 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['Parameter']['Value'])" 2>/dev/null); then
    GOOGLE_OAUTH_CLIENT_ID="$_GOAUTH_ID"
    ok "Google OAuth client ID loaded from SSM"
  else
    warn "Could not fetch Google OAuth client ID from SSM — catalog Drive sync disabled"
  fi
  if _GOAUTH_SECRET=$(aws ssm get-parameter --name /nannos/infrastructure-agents/google-oauth-client-secret --output json --with-decryption 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['Parameter']['Value'])" 2>/dev/null); then
    GOOGLE_OAUTH_CLIENT_SECRET="$_GOAUTH_SECRET"
    ok "Google OAuth client secret loaded from SSM"
  else
    warn "Could not fetch Google OAuth client secret from SSM"
  fi

  # Twilio credentials (optional — needed for voice agent + phone verification)
  if _TWILIO_SID=$(aws ssm get-parameter --name /nannos/twilio/account-sid --output json --with-decryption 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['Parameter']['Value'])" 2>/dev/null); then
    TWILIO_ACCOUNT_SID="$_TWILIO_SID"
    ok "Twilio Account SID loaded from SSM"
  else
    warn "Could not fetch Twilio Account SID from SSM — voice calls and phone verification disabled"
  fi
  if _TWILIO_KEY=$(aws ssm get-parameter --name /nannos/twilio/api-key --output json --with-decryption 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['Parameter']['Value'])" 2>/dev/null); then
    TWILIO_API_KEY="$_TWILIO_KEY"
  fi
  if _TWILIO_SECRET=$(aws ssm get-parameter --name /nannos/twilio/api-secret --output json --with-decryption 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['Parameter']['Value'])" 2>/dev/null); then
    TWILIO_API_SECRET="$_TWILIO_SECRET"
  fi
  if _TWILIO_VSID=$(aws ssm get-parameter --name /nannos/twilio/verify-service-sid --output json --with-decryption 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['Parameter']['Value'])" 2>/dev/null); then
    TWILIO_VERIFY_SERVICE_SID="$_TWILIO_VSID"
  fi
  if _TWILIO_VKEY=$(aws ssm get-parameter --name /nannos/twilio/verify-api-key --output json --with-decryption 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['Parameter']['Value'])" 2>/dev/null); then
    TWILIO_VERIFY_API_KEY="$_TWILIO_VKEY"
  fi
  if _TWILIO_VSECRET=$(aws ssm get-parameter --name /nannos/twilio/verify-api-secret --output json --with-decryption 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['Parameter']['Value'])" 2>/dev/null); then
    TWILIO_VERIFY_API_SECRET="$_TWILIO_VSECRET"
  fi

  ok "AWS resources configured (dev environment)"
fi

# ── OIDC configuration ──
_OIDC_ISSUER="http://localhost:8180/realms/nannos"
_OIDC_SECRET_BACKEND="local-secret"
_OIDC_SECRET_ORCHESTRATOR="local-secret"
_OIDC_SECRET_CREATOR="local-secret"
_OIDC_SECRET_ADMIN="local-secret"
_KC_BASE_URL="http://localhost:8180"
_KC_REALM="nannos"

if [[ "$_OIDC_MODE" == "remote-ssm" ]]; then
  _OIDC_ISSUER="$OIDC_ISSUER"
  _KC_BASE_URL="${OIDC_ISSUER%/realms/*}"
  _KC_REALM="${OIDC_ISSUER##*/realms/}"

  log "Fetching OIDC secrets from AWS SSM..."

  if _secret=$(aws ssm get-parameter --name /nannos/keycloak/agent-console-client-secret --output json --with-decryption 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['Parameter']['Value'])" 2>/dev/null); then
    _OIDC_SECRET_BACKEND="$_secret"
  else
    err "Failed to fetch agent-console OIDC secret from SSM"
  fi

  if _secret=$(aws ssm get-parameter --name /nannos/keycloak/orchestrator-secret --output json --with-decryption 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['Parameter']['Value'])" 2>/dev/null); then
    _OIDC_SECRET_ORCHESTRATOR="$_secret"
  else
    err "Failed to fetch orchestrator OIDC secret from SSM"
  fi

  if _secret=$(aws ssm get-parameter --name /nannos/keycloak/agent-creator-secret --output json --with-decryption 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['Parameter']['Value'])" 2>/dev/null); then
    _OIDC_SECRET_CREATOR="$_secret"
  else
    err "Failed to fetch agent-creator OIDC secret from SSM"
  fi

  if _secret=$(aws ssm get-parameter --name /nannos/keycloak/nannos-admin-secret --output json --with-decryption 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['Parameter']['Value'])" 2>/dev/null); then
    _OIDC_SECRET_ADMIN="$_secret"
  else
    warn "Could not fetch nannos-admin secret — Keycloak group sync disabled"
    _OIDC_SECRET_ADMIN=""
  fi

  ok "OIDC secrets loaded from SSM"

elif [[ "$_OIDC_MODE" == "remote-manual" ]]; then
  _OIDC_ISSUER="$OIDC_ISSUER"
  _KC_BASE_URL="${OIDC_ISSUER%/realms/*}"
  _KC_REALM="${OIDC_ISSUER##*/realms/}"

  printf "${CYAN}▸ Enter OIDC client secret (shared for all services): ${RESET}"
  read -r _shared_secret
  if [[ -z "$_shared_secret" ]]; then
    err "OIDC client secret is required when using external OIDC without AWS_PROFILE"
  fi
  _OIDC_SECRET_BACKEND="$_shared_secret"
  _OIDC_SECRET_ORCHESTRATOR="$_shared_secret"
  _OIDC_SECRET_CREATOR="$_shared_secret"
  _OIDC_SECRET_ADMIN="$_shared_secret"
  ok "OIDC configured with shared secret"
fi

# ─── 4. Start infrastructure (PostgreSQL + Keycloak) ──────────────

log "Starting infrastructure..."

cd "$LOCAL_DEV_DIR"
docker compose up -d

# Wait for PostgreSQL
log "Waiting for PostgreSQL..."
until docker compose exec -T postgres-console pg_isready -U postgres >/dev/null 2>&1; do
  sleep 1
done
until docker compose exec -T postgres-docstore pg_isready -U postgres >/dev/null 2>&1; do
  sleep 1
done
ok "PostgreSQL is ready"

# ─── 5. Run database migrations ──────────────────────────────────

log "Running database migrations (Rambler)..."

CONSOLE_MIGRATIONS_IMAGE="nannos-console-migrations:local"
DOCSTORE_MIGRATIONS_IMAGE="nannos-docstore-migrations:local"

# Build both migration images
docker build -t "$CONSOLE_MIGRATIONS_IMAGE" "$ROOT_DIR/packages/console-backend/sqlmigrations" --quiet
docker build -t "$DOCSTORE_MIGRATIONS_IMAGE" "$ROOT_DIR/packages/orchestrator-agent/sqlmigrations" --quiet

# Install pgvector extension on the docstore database
docker run --rm \
  --network host \
  --user 0 \
  --entrypoint psql \
  -e PGPASSWORD=password \
  "$DOCSTORE_MIGRATIONS_IMAGE" "postgresql://postgres:password@127.0.0.1:5402/docstore" -c "
    CREATE EXTENSION IF NOT EXISTS vector;
  "

# Run console-backend migrations (public schema on port 5401)
log "Applying console-backend migrations (db: console, port: 5401)..."
docker run --rm \
  --network host \
  --user 0 \
  -e PGHOST=127.0.0.1 \
  -e PGPORT=5401 \
  -e PGUSER=postgres \
  -e PGPASSWORD=password \
  -e PGDATABASE=console \
  -e PGSCHEMA=public \
  -e RAMBLER_SSLMODE=disable \
  "$CONSOLE_MIGRATIONS_IMAGE"

# Run orchestrator-agent migrations (public schema on port 5402)
log "Applying orchestrator-agent migrations (db: docstore, port: 5402)..."
docker run --rm \
  --network host \
  --user 0 \
  -e PGHOST=127.0.0.1 \
  -e PGPORT=5402 \
  -e PGUSER=postgres \
  -e PGPASSWORD=password \
  -e PGDATABASE=docstore \
  -e PGSCHEMA=public \
  -e RAMBLER_SSLMODE=disable \
  "$DOCSTORE_MIGRATIONS_IMAGE"

ok "Database migrations applied"

# Seed: make all users administrators for local dev
docker compose exec -T postgres-console psql -U postgres -d console -c \
  "UPDATE users SET is_administrator = true WHERE is_administrator = false;" \
  >/dev/null 2>&1 || true

# ─── 6. Wait for Keycloak ────────────────────────────────────────

if [[ "$_OIDC_MODE" == "local" ]]; then

log "Waiting for Keycloak..."
KEYCLOAK_RETRIES=0
until curl -sf http://localhost:8180/realms/nannos/.well-known/openid-configuration >/dev/null 2>&1; do
  KEYCLOAK_RETRIES=$((KEYCLOAK_RETRIES + 1))
  if [[ $KEYCLOAK_RETRIES -ge 60 ]]; then
    err "Keycloak did not become ready after 60s. Check: docker compose logs keycloak"
  fi
  sleep 1
done
ok "Keycloak is ready (realm: nannos)"

# ─── 6b. Fix Keycloak client secrets ─────────────────────────────
# Keycloak ignores the "secret" field in realm exports and generates random ones.
# We force all confidential clients to use "local-secret" via the Admin API.

log "Configuring Keycloak client secrets..."

KC_ADMIN_TOKEN=""
for i in $(seq 1 15); do
  KC_ADMIN_TOKEN=$(curl -s -X POST http://localhost:8180/realms/master/protocol/openid-connect/token \
    -d "grant_type=password" \
    -d "client_id=admin-cli" \
    -d "username=admin" \
    -d "password=admin" \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('access_token',''))" 2>/dev/null || true)
  [[ -n "$KC_ADMIN_TOKEN" ]] && break
  sleep 2
done

if [[ -z "$KC_ADMIN_TOKEN" ]]; then
  err "Failed to obtain Keycloak admin token after retries. Check: docker compose logs keycloak"
fi

for CLIENT_ID in agent-console orchestrator agent-creator nannos-admin; do
  # Get the internal UUID for this client
  CLIENT_UUID=$(curl -sf -H "Authorization: Bearer $KC_ADMIN_TOKEN" \
    "http://localhost:8180/admin/realms/nannos/clients?clientId=$CLIENT_ID" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")

  # Set the secret to "local-secret"
  curl -sf -X PUT -H "Authorization: Bearer $KC_ADMIN_TOKEN" -H "Content-Type: application/json" \
    "http://localhost:8180/admin/realms/nannos/clients/$CLIENT_UUID" \
    -d "{\"secret\": \"local-secret\"}" >/dev/null

done

ok "Keycloak client secrets configured"

else
  log "Skipping local Keycloak (using external OIDC)"
fi  # _OIDC_MODE

# ─── 7. Install dependencies ─────────────────────────────────────

log "Installing Python dependencies..."
cd "$ROOT_DIR"

# Sync all Python packages in parallel
for pkg in orchestrator-agent agent-creator agent-runner console-backend; do
  (cd "packages/$pkg" && uv sync --quiet) &
done
(cd "packages/voice-agent" && uv sync --quiet) &
wait
ok "Python dependencies installed"

log "Installing frontend dependencies..."
cd "$ROOT_DIR/packages/console-frontend"
npm install --silent 2>/dev/null
ok "Frontend dependencies installed"

# ─── 8. Launch services via mprocs ─────────────────────────────────

cd "$ROOT_DIR"

log "Starting all services with mprocs..."
printf "\n"

# ── Create logs directory ──
_LOG_DIR="$ROOT_DIR/logs"
mkdir -p "$_LOG_DIR"

# ── Resolve optional env vars ──
export OPENAI_COMPATIBLE_BASE_URL="${OPENAI_COMPATIBLE_BASE_URL:-}"
export OPENAI_COMPATIBLE_MODEL="${OPENAI_COMPATIBLE_MODEL:-}"
export MCP_GATEWAY_URL="${MCP_GATEWAY_URL:-}"
export MCP_GATEWAY_CLIENT_ID="${MCP_GATEWAY_CLIENT_ID:-gatana}"
export LANGSMITH_TRACING="${LANGSMITH_TRACING:-false}"
export LANGSMITH_API_KEY="${LANGSMITH_API_KEY:-}"
export LANGSMITH_PROJECT="${LANGSMITH_PROJECT:-}"
export LANGSMITH_ENDPOINT="${LANGSMITH_ENDPOINT:-}"
export CATALOG_VECTOR_BUCKET_NAME="${CATALOG_VECTOR_BUCKET_NAME:-}"
export CATALOG_THUMBNAILS_S3_BUCKET="${CATALOG_THUMBNAILS_S3_BUCKET:-}"
export GOOGLE_OAUTH_CLIENT_ID="${GOOGLE_OAUTH_CLIENT_ID:-}"
export GOOGLE_OAUTH_CLIENT_SECRET="${GOOGLE_OAUTH_CLIENT_SECRET:-}"
export TWILIO_ACCOUNT_SID="${TWILIO_ACCOUNT_SID:-}"
export TWILIO_API_KEY="${TWILIO_API_KEY:-}"
export TWILIO_API_SECRET="${TWILIO_API_SECRET:-}"
export TWILIO_VERIFY_SERVICE_SID="${TWILIO_VERIFY_SERVICE_SID:-}"
export TWILIO_VERIFY_API_KEY="${TWILIO_VERIFY_API_KEY:-}"
export TWILIO_VERIFY_API_SECRET="${TWILIO_VERIFY_API_SECRET:-}"

# ── Generate mprocs config ──
MPROCS_CFG=$(mktemp /tmp/nannos-mprocs-XXXXXX)
mv "$MPROCS_CFG" "${MPROCS_CFG}.yaml"
MPROCS_CFG="${MPROCS_CFG}.yaml"

# Build scenario label for info panel
if [[ "$_OIDC_MODE" == "remote-ssm" ]]; then
  _SCENARIO="Local + AWS + Remote OIDC"
elif [[ "$_OIDC_MODE" == "remote-manual" ]]; then
  _SCENARIO="Local + Remote OIDC"
elif [[ "$_HAS_AWS" == true ]]; then
  _SCENARIO="Local + AWS"
else
  _SCENARIO="Full Local"
fi

# Build LLM lines
_LLM_LINES=""
if [[ "$_HAS_LOCAL_LLM" == true ]]; then
  _line="Local LLM: ${OPENAI_COMPATIBLE_BASE_URL:-}"
  [[ -n "${OPENAI_COMPATIBLE_MODEL:-}" ]] && _line="$_line (model: $OPENAI_COMPATIBLE_MODEL)"
  _LLM_LINES="    ✓ $_line"$'\n'
fi
if [[ "$_HAS_AWS" == true ]]; then
  _LLM_LINES="${_LLM_LINES}    ✓ AWS Bedrock (region: $AWS_BEDROCK_REGION)"$'\n'
  [[ -n "$AZURE_OPENAI_API_KEY" ]] && _LLM_LINES="${_LLM_LINES}    ✓ Azure OpenAI"$'\n'
  [[ -n "$GCP_KEY" ]]              && _LLM_LINES="${_LLM_LINES}    ✓ GCP Vertex AI"$'\n'
fi

# Build auth line
if [[ "$_OIDC_MODE" == "local" ]]; then
  _AUTH_LINE="Local Keycloak (localhost:8180) — test@local.dev / password"
else
  _AUTH_LINE="Remote OIDC: $_OIDC_ISSUER"
fi

# Build optional lines
_OPT_LINES=""
[[ "$_HAS_MCP" == true ]] && _OPT_LINES="${_OPT_LINES}    ✓ MCP Gateway: $MCP_GATEWAY_URL"$'\n'
[[ -n "${LANGSMITH_API_KEY:-}" ]] && _OPT_LINES="${_OPT_LINES}    ✓ LangSmith tracing enabled"$'\n'

# Build debug lines
_DEBUG_LINES=""
if [[ -n "$_DEBUG_MODE" ]]; then
  _DEBUG_LINES="  Debugging (debugpy):
    backend .......... localhost:5678
    orchestrator ..... localhost:5679
    creator .......... localhost:5680
    runner ........... localhost:5682
    voice-agent ...... localhost:5683
"
fi

# Prepare Slack
pushd "$ROOT_DIR/packages/client-slack"
just prepare-start
popd

# Generate the info script
_INFO_SCRIPT=$(mktemp /tmp/nannos-info-XXXXXX)
mv "$_INFO_SCRIPT" "${_INFO_SCRIPT}.sh"
_INFO_SCRIPT="${_INFO_SCRIPT}.sh"
cat > "$_INFO_SCRIPT" <<INFOSCRIPT
#!/usr/bin/env bash
cat <<'EOF'

  ┌──────────────────────────────────────────────────────────┐
  │  Nannos Local Development                                │
  └──────────────────────────────────────────────────────────┘

  Scenario: $_SCENARIO

  Services:
    Console ........... http://localhost:5173
    Backend API ....... http://localhost:5001
    Orchestrator ...... http://localhost:10001
    Agent Creator ..... http://localhost:8080
    Agent Runner ...... http://localhost:5005
    Voice Agent ....... http://localhost:8002
    Keycloak .......... $_KC_BASE_URL
    PostgreSQL (console) . localhost:5401
    PostgreSQL (docstore)  localhost:5402

  LLM Providers:
${_LLM_LINES}
  Authentication:
    ✓ $_AUTH_LINE
${_OPT_LINES:+
  Optional:
$_OPT_LINES}
${_DEBUG_LINES}
  ──────────────────────────────────────────────────────────

  Getting Started:
    1. Open http://localhost:5173 in your browser
    2. Log in with the credentials shown above
    3. Create or select an agent from the console
    4. Start chatting!

  Tips:
    • Use the mprocs tabs above to switch between service logs
    • Services auto-reload when you edit code
    • Check individual service tabs if something looks wrong
    • Press Ctrl+C or 'q' in mprocs to stop everything
    • Log files: $_LOG_DIR/<service>.log

  ──────────────────────────────────────────────────────────
  MPROCS CONFIG: $MPROCS_CFG

EOF
sleep 30d
INFOSCRIPT
chmod +x "$_INFO_SCRIPT"

cat > "$MPROCS_CFG" <<YAML
procs:
  info:
    shell: "bash $_INFO_SCRIPT"
    stop: "SIGKILL"

  console-backend:
    cwd: "$ROOT_DIR/packages/console-backend"
    shell: "uv run python${_DEBUG_MODE:+ -m debugpy --listen 0.0.0.0:5678} -m uvicorn app:asgi_app --host 127.0.0.1 --port 5001 --reload 2>&1 | tee $_LOG_DIR/console-backend.log"
    env:
      OIDC_ISSUER: "$_OIDC_ISSUER"
      OIDC_CLIENT_ID: "agent-console"
      OIDC_CLIENT_SECRET: "$_OIDC_SECRET_BACKEND"
      OIDC_AUDIENCE: "agent-console"
      ORCHESTRATOR_CLIENT_ID: "orchestrator"
      ORCHESTRATOR_BASE_DOMAIN: "localhost:10001"
      ORCHESTRATOR_ENVIRONMENT: "local"
      KEYCLOAK_ADMIN_CLIENT_ID: "nannos-admin"
      KEYCLOAK_ADMIN_CLIENT_SECRET: "$_OIDC_SECRET_ADMIN"
      KEYCLOAK_GROUP_NAME_PREFIX: "local-"
      POSTGRES_HOST: "localhost"
      POSTGRES_PORT: "5401"
      POSTGRES_DB: "console"
      POSTGRES_USER: "postgres"
      POSTGRES_PASSWORD: "password"
      POSTGRES_SCHEMA: "public"
      SCHEDULER_TICK_INTERVAL_SECONDS: "30"
      SCHEDULER_CLAIM_LIMIT: "10"
      AGENT_RUNNER_URL: "http://localhost:5005"
      LOG_LEVEL: "INFO"
      OPENAI_COMPATIBLE_BASE_URL: "$OPENAI_COMPATIBLE_BASE_URL"
      AZURE_OPENAI_API_KEY: "$AZURE_OPENAI_API_KEY"
      AZURE_OPENAI_ENDPOINT: "$AZURE_OPENAI_ENDPOINT"
      BEDROCK_MODEL_ID: "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
      AWS_BEDROCK_REGION: "$AWS_BEDROCK_REGION"
      CHECKPOINT_DYNAMODB_TABLE_NAME: "$CHECKPOINT_DYNAMODB_TABLE_NAME"
      CHECKPOINT_S3_BUCKET_NAME: "$CHECKPOINT_S3_BUCKET_NAME"
      FILES_S3_BUCKET: "$FILES_S3_BUCKET"
      VOICE_AGENT_URL: "http://localhost:8002"
      AGENT_CREATOR_URL: "http://localhost:8080"
      CATALOG_VECTOR_BUCKET_NAME: "$CATALOG_VECTOR_BUCKET_NAME"
      CATALOG_THUMBNAILS_S3_BUCKET: "$CATALOG_THUMBNAILS_S3_BUCKET"
      CATALOG_VECTOR_STORE_BACKEND: "s3_vectors"
      CATALOG_SUMMARIZATION_MODEL_ID: "global.anthropic.claude-haiku-4-5-20251001-v1:0"
      CATALOG_AUTO_SYNC_ENABLED: "true"
      CATALOG_SYNC_INTERVAL_SECONDS: "86400"
      CATALOG_SYNC_TICK_INTERVAL_SECONDS: "300"
      CATALOG_SYNC_MAX_CONCURRENT: "3"
      GOOGLE_OAUTH_CLIENT_ID: "$GOOGLE_OAUTH_CLIENT_ID"
      GOOGLE_OAUTH_CLIENT_SECRET: "$GOOGLE_OAUTH_CLIENT_SECRET"
      GOOGLE_OAUTH_REDIRECT_URI: "http://localhost:5001/api/v1/catalogs/connect/callback"
      GCP_KEY: '$GCP_KEY'
      GCP_PROJECT_ID: "$GCP_PROJECT_ID"
      TWILIO_ACCOUNT_SID: "$TWILIO_ACCOUNT_SID"
      TWILIO_VERIFY_SERVICE_SID: "$TWILIO_VERIFY_SERVICE_SID"
      TWILIO_VERIFY_API_KEY: "$TWILIO_VERIFY_API_KEY"
      TWILIO_VERIFY_API_SECRET: "$TWILIO_VERIFY_API_SECRET"

  orchestrator:
    cwd: "$ROOT_DIR/packages/orchestrator-agent"
    shell: "uv run python${_DEBUG_MODE:+ -m debugpy --listen 0.0.0.0:5679} main.py --host 0.0.0.0 --port 10001 --reload 2>&1 | tee $_LOG_DIR/orchestrator.log"
    env:
      OIDC_ISSUER: "$_OIDC_ISSUER"
      OIDC_CLIENT_ID: "orchestrator"
      OIDC_CLIENT_SECRET: "$_OIDC_SECRET_ORCHESTRATOR"
      ORCHESTRATOR_CLIENT_ID: "orchestrator"
      AGENT_CLIENT_ID: "agent-creator"
      AGENT_ID: "1"
      AGENT_BASE_URL: "http://localhost:10001"
      PLAYGROUND_BACKEND_URL: "http://localhost:5001"
      PLAYGROUND_FRONTEND_URL: "http://localhost:5173"
      POSTGRES_HOST: "localhost"
      POSTGRES_PORT: "5402"
      POSTGRES_DB: "docstore"
      POSTGRES_USER: "postgres"
      POSTGRES_PASSWORD: "password"
      POSTGRES_SCHEMA: "public"
      OPENAI_COMPATIBLE_BASE_URL: "$OPENAI_COMPATIBLE_BASE_URL"
      OPENAI_COMPATIBLE_MODEL: "$OPENAI_COMPATIBLE_MODEL"
      MCP_GATEWAY_URL: "$MCP_GATEWAY_URL"
      MCP_GATEWAY_CLIENT_ID: "$MCP_GATEWAY_CLIENT_ID"
      LANGSMITH_TRACING: "$LANGSMITH_TRACING"
      LANGSMITH_API_KEY: "$LANGSMITH_API_KEY"
      LANGSMITH_PROJECT: "$LANGSMITH_PROJECT"
      LANGSMITH_ENDPOINT: "$LANGSMITH_ENDPOINT"
      LOG_LEVEL: "INFO"
      BUDGET_ENABLED: "false"
      USE_SHORT_PROMPTS: "true"
      AZURE_OPENAI_API_KEY: "$AZURE_OPENAI_API_KEY"
      AZURE_OPENAI_ENDPOINT: "$AZURE_OPENAI_ENDPOINT"
      AWS_BEDROCK_REGION: "$AWS_BEDROCK_REGION"
      BEDROCK_MODEL_ID: "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
      GCP_KEY: '$GCP_KEY'
      GCP_PROJECT_ID: "$GCP_PROJECT_ID"
      GCP_LOCATION: "$GCP_LOCATION"
      CHECKPOINT_DYNAMODB_TABLE_NAME: "$CHECKPOINT_DYNAMODB_TABLE_NAME"
      CHECKPOINT_S3_BUCKET_NAME: "$CHECKPOINT_S3_BUCKET_NAME"
      CHECKPOINT_AWS_REGION: "$CHECKPOINT_AWS_REGION"
      DOCUMENT_STORE_S3_BUCKET: "$DOCUMENT_STORE_S3_BUCKET"
      CATALOG_VECTOR_BUCKET_NAME: "$CATALOG_VECTOR_BUCKET_NAME"
      CATALOG_THUMBNAILS_S3_BUCKET: "$CATALOG_THUMBNAILS_S3_BUCKET"

  creator:
    cwd: "$ROOT_DIR/packages/agent-creator"
    shell: "uv run python${_DEBUG_MODE:+ -m debugpy --listen 0.0.0.0:5680} main.py --host 0.0.0.0 --port 8080 --reload 2>&1 | tee $_LOG_DIR/creator.log"
    env:
      OIDC_ISSUER: "$_OIDC_ISSUER"
      OIDC_CLIENT_ID: "agent-creator"
      OIDC_CLIENT_SECRET: "$_OIDC_SECRET_CREATOR"
      ORCHESTRATOR_CLIENT_ID: "orchestrator"
      AGENT_CLIENT_ID: "agent-creator"
      AGENT_ID: "1"
      AGENT_BASE_URL: "http://localhost:8080"
      PLAYGROUND_BACKEND_URL: "http://localhost:5001"
      PLAYGROUND_FRONTEND_URL: "http://localhost:5173"
      OPENAI_COMPATIBLE_BASE_URL: "$OPENAI_COMPATIBLE_BASE_URL"
      OPENAI_COMPATIBLE_MODEL: "$OPENAI_COMPATIBLE_MODEL"
      LANGSMITH_TRACING: "$LANGSMITH_TRACING"
      LANGSMITH_API_KEY: "$LANGSMITH_API_KEY"
      LANGSMITH_PROJECT: "$LANGSMITH_PROJECT"
      LANGSMITH_ENDPOINT: "$LANGSMITH_ENDPOINT"
      LOG_LEVEL: "DEBUG"
      AZURE_OPENAI_API_KEY: "$AZURE_OPENAI_API_KEY"
      AZURE_OPENAI_ENDPOINT: "$AZURE_OPENAI_ENDPOINT"
      AWS_BEDROCK_REGION: "$AWS_BEDROCK_REGION"
      BEDROCK_MODEL_ID: "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
      CHECKPOINT_DYNAMODB_TABLE_NAME: "$CHECKPOINT_DYNAMODB_TABLE_NAME"
      CHECKPOINT_S3_BUCKET_NAME: "$CHECKPOINT_S3_BUCKET_NAME"
      CHECKPOINT_AWS_REGION: "$CHECKPOINT_AWS_REGION"

  runner:
    cwd: "$ROOT_DIR/packages/agent-runner"
    shell: "uv run python${_DEBUG_MODE:+ -m debugpy --listen 0.0.0.0:5682} main.py --host 0.0.0.0 --port 5005 --reload 2>&1 | tee $_LOG_DIR/runner.log"
    env:
      OIDC_ISSUER: "$_OIDC_ISSUER"
      OIDC_CLIENT_ID: "orchestrator"
      OIDC_CLIENT_SECRET: "$_OIDC_SECRET_ORCHESTRATOR"
      SCHEDULER_SERVICE_CLIENT_ID: "agent-console"
      AGENT_BASE_URL: "http://localhost:5005"
      PLAYGROUND_BACKEND_URL: "http://localhost:5001"
      POSTGRES_HOST: "localhost"
      POSTGRES_PORT: "5402"
      POSTGRES_DB: "docstore"
      POSTGRES_USER: "postgres"
      POSTGRES_PASSWORD: "password"
      POSTGRES_SCHEMA: "public"
      OPENAI_COMPATIBLE_BASE_URL: "$OPENAI_COMPATIBLE_BASE_URL"
      OPENAI_COMPATIBLE_MODEL: "$OPENAI_COMPATIBLE_MODEL"
      MCP_GATEWAY_URL: "$MCP_GATEWAY_URL"
      MCP_GATEWAY_CLIENT_ID: "$MCP_GATEWAY_CLIENT_ID"
      LANGSMITH_TRACING: "$LANGSMITH_TRACING"
      LANGSMITH_API_KEY: "$LANGSMITH_API_KEY"
      LANGSMITH_PROJECT: "$LANGSMITH_PROJECT"
      LANGSMITH_ENDPOINT: "$LANGSMITH_ENDPOINT"
      LOG_LEVEL: "INFO"
      AZURE_OPENAI_API_KEY: "$AZURE_OPENAI_API_KEY"
      AZURE_OPENAI_ENDPOINT: "$AZURE_OPENAI_ENDPOINT"
      AWS_BEDROCK_REGION: "$AWS_BEDROCK_REGION"
      BEDROCK_MODEL_ID: "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
      GCP_KEY: '$GCP_KEY'
      GCP_PROJECT_ID: "$GCP_PROJECT_ID"
      GCP_LOCATION: "$GCP_LOCATION"
      CHECKPOINT_DYNAMODB_TABLE_NAME: "$CHECKPOINT_DYNAMODB_TABLE_NAME"
      CHECKPOINT_S3_BUCKET_NAME: "$CHECKPOINT_S3_BUCKET_NAME"
      CHECKPOINT_AWS_REGION: "$CHECKPOINT_AWS_REGION"
      DOCUMENT_STORE_S3_BUCKET: "$DOCUMENT_STORE_S3_BUCKET"

  voice-agent:
    cwd: "$ROOT_DIR/packages/voice-agent"
    shell: "uv run python${_DEBUG_MODE:+ -m debugpy --listen 0.0.0.0:5683} main.py  --reload 2>&1 | tee $_LOG_DIR/voice-agent.log"
    env:
      HOST: "localhost"
      PORT: "8002"
      OIDC_ISSUER: "$_OIDC_ISSUER"
      OIDC_CLIENT_ID: "voice-agent"
      VOICE_AGENT_BASE_URL: "http://localhost:8002"
      PLAYGROUND_BACKEND_URL: "http://localhost:5001"
      PUBLIC_URL: "${PUBLIC_URL:-}"
      GCP_KEY: '$GCP_KEY'
      GCP_PROJECT_ID: "$GCP_PROJECT_ID"
      GCP_LOCATION: "$GCP_LOCATION"
      CALL_TIMEOUT_SECONDS: "600"
      TWILIO_ACCOUNT_SID: "$TWILIO_ACCOUNT_SID"
      TWILIO_API_KEY: "$TWILIO_API_KEY"
      TWILIO_API_SECRET: "$TWILIO_API_SECRET"
      TWILIO_PHONE_NUMBER: "+358454917751"
      TWILIO_REGION: "ie1"
      TWILIO_EDGE: "dublin"
      LANGSMITH_TRACING: "$LANGSMITH_TRACING"
      LANGSMITH_API_KEY: "$LANGSMITH_API_KEY"
      LANGSMITH_PROJECT: "$LANGSMITH_PROJECT"
      LANGSMITH_ENDPOINT: "$LANGSMITH_ENDPOINT"
      LOG_LEVEL: "INFO"

  frontend:
    cwd: "$ROOT_DIR/packages/console-frontend"
    shell: "npx vite --host 0.0.0.0 --port 5173 2>&1 | tee $_LOG_DIR/frontend.log"
    env:
      VITE_API_BASE_URL: "http://localhost:5001"
      VITE_ORCHESTRATOR_BASE_DOMAIN: "localhost:10001"
      VITE_KEYCLOAK_BASE_URL: "$_KC_BASE_URL"
      VITE_KEYCLOAK_REALM: "$_KC_REALM"
      VITE_AUTO_APPROVE_MAX_SYSTEM_PROMPT_LENGTH: "500"
      VITE_AUTO_APPROVE_MAX_MCP_TOOLS_COUNT: "3"

  infra-logs:
    cwd: "$LOCAL_DEV_DIR"
    shell: "docker compose logs -f 2>&1 | tee $_LOG_DIR/infra.log"
    stop: "SIGKILL"
  
  slack:
    cwd: "$ROOT_DIR/packages/client-slack"
    shell: "just start 2>&1 | tee $_LOG_DIR/slack.log"
    stop: "SIGKILL"
YAML

exec mprocs --config "$MPROCS_CFG"

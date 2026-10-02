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
#   --debug      Start Python services with debugpy for VS Code debugging
#                (ports: backend=5678, orchestrator=5679,
#                 runner=5682, voice-agent=5683; offset per slot)
#   --slot N     Run as stack slot N (1-8) beside slot 0 and the others (ADR-0016), always
#                with --headless. Claimed and driven by `just up` / `just down`; not by hand.
#   --headless   Start the services in the background instead of mprocs, wait until they are
#                healthy, write the slot's JSON summary and exit (slots only).
#   --local-idp  Use the local Keycloak even when .env names a remote OIDC_ISSUER.
#   --from-slot0 Start a slot's new databases as a copy of slot 0's (its agents, models,
#                users, conversations), then apply this worktree's pending migrations.
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
#   AUTO_APPROVE_MAX_SYSTEM_PROMPT_LENGTH - auto-approve prompt limit (defaults to 500;
#                              set it low to exercise the pending-approval paths)
#   AUTO_APPROVE_MAX_MCP_TOOLS_COUNT      - auto-approve MCP tool limit (defaults to 3)
#
# Cloud LLM providers (fetched from SSM when AWS_PROFILE is set; a value in .env wins):
#   AZURE_AI_API_KEY         - key of the Nannos Azure AI Foundry resource, used for both
#                              azure/ and azure_ai/ models (AZURE_OPENAI_API_KEY defaults to it)
#   AZURE_API_BASE           - endpoint for azure/<deployment> models (defaults to the Foundry
#                              resource's OpenAI host; bare host, no /openai/v1)
#   AZURE_AI_API_BASE        - endpoint for azure_ai/<model> models (defaults to the same resource)
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
_SLOT=0
_HEADLESS=""
_FORCE_LOCAL_IDP=""
_FROM_SLOT0=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --debug) _DEBUG_MODE=1; shift ;;
    --slot) _SLOT="${2:-}"; shift 2 ;;
    --headless) _HEADLESS=1; shift ;;
    --local-idp) _FORCE_LOCAL_IDP=1; shift ;;
    --from-slot0) _FROM_SLOT0=1; shift ;;
    *) echo "Unknown flag: $1"; exit 1 ;;
  esac
done
if [[ ! "$_SLOT" =~ ^[0-8]$ ]]; then
  echo "--slot takes 1-8 (slot 0 is the default stack)"; exit 1
fi
if [[ -n "$_FROM_SLOT0" && "$_SLOT" == 0 ]]; then
  echo "--from-slot0 starts a slot (1-8) from slot 0's databases; use 'just up --from-slot0'"; exit 1
fi
if [[ -n "$_HEADLESS" && "$_SLOT" == 0 ]]; then
  echo "--headless runs a slot (1-8); use 'just up'"; exit 1
fi
# A slot runs headless only: its claim is alive through the PIDs headless mode records, so a
# slot under mprocs would look dead to `just slots-gc` and be dropped while running.
if [[ "$_SLOT" != 0 && -z "$_HEADLESS" ]]; then
  echo "A slot runs headless; start it with 'just up'"; exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOCAL_DEV_DIR="$SCRIPT_DIR/local-dev"
# shellcheck source=local-dev/slot-common.sh
source "$LOCAL_DEV_DIR/slot-common.sh"

# Port console-backend listens on, and that every other service is pointed at.
# Overridable for when something else already holds the default:
#   CONSOLE_BACKEND_PORT=5002 ./scripts/start-local.sh
# Exported so the frontend's vite proxy picks the same value up.
export CONSOLE_BACKEND_PORT="${CONSOLE_BACKEND_PORT:-5001}"

# Source .env from repo root if present. A fresh worktree has none (it is gitignored):
# `just start-local` and `just up` copy it from the main checkout first (`just env-sync`).
_DOTENV_LOADED=false
if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  source "$ROOT_DIR/.env"
  set +a
  _DOTENV_LOADED=true
fi
if [[ -n "$_FORCE_LOCAL_IDP" ]]; then
  unset OIDC_ISSUER
fi

# ─── 0b. Stack slot (ADR-0016) ─────────────────────────────────────
# Slot 0 is the stack this script has always started. Slots 1-8 run beside it and each other:
# slot N owns the port block 4N000-4N999, its own databases on the shared Postgres servers, its
# own Model Gateway container, its own cookie names and IdP group prefix.
if [[ "$_SLOT" == 0 ]]; then
  _P_FRONTEND=5173; _P_ORCHESTRATOR=10001; _P_RUNNER=5005; _P_VOICE=8002; _P_SOFFICE=8090
  _P_DBG_BACKEND=5678; _P_DBG_ORCHESTRATOR=5679; _P_DBG_RUNNER=5682; _P_DBG_VOICE=5683
  _GW_CONTAINER="nannos-litellm-proxy-local"
  _GROUP_PREFIX="local-"
  _LOG_DIR="$ROOT_DIR/logs"
else
  _SLOT_DIR="$NANNOS_SLOTS_DIR/$_SLOT"
  if [[ ! -f "$_SLOT_DIR/claim.json" ]]; then
    echo "Slot $_SLOT is not claimed. Start a slot with 'just up'."; exit 1
  fi
  export CONSOLE_BACKEND_PORT="$(slot_port "$_SLOT" backend)"
  LLM_GATEWAY_PORT="$(slot_port "$_SLOT" gateway)"
  _P_FRONTEND="$(slot_port "$_SLOT" frontend)"; _P_ORCHESTRATOR="$(slot_port "$_SLOT" orchestrator)"
  _P_RUNNER="$(slot_port "$_SLOT" runner)"; _P_VOICE="$(slot_port "$_SLOT" voice)"
  _P_SOFFICE="$(slot_port "$_SLOT" soffice)"
  _P_DBG_BACKEND="$(slot_port "$_SLOT" dbg_backend)"; _P_DBG_ORCHESTRATOR="$(slot_port "$_SLOT" dbg_orchestrator)"
  _P_DBG_RUNNER="$(slot_port "$_SLOT" dbg_runner)"; _P_DBG_VOICE="$(slot_port "$_SLOT" dbg_voice)"
  _GW_CONTAINER="nannos-gw-s${_SLOT}"
  _GROUP_PREFIX="local-s${_SLOT}-"
  _LOG_DIR="$_SLOT_DIR/logs"
  # Browsers scope cookies by host, not port: without their own names, slots on localhost would
  # sign each other out. The browser's Origin is the slot's frontend, which Socket.IO checks.
  export SESSION_COOKIE_NAME="a2a-chatui-s${_SLOT}"
  export OAUTH_STATE_COOKIE_NAME="session-s${_SLOT}"
  export CORS_ALLOWED_CHAT_ORIGINS="${CORS_ALLOWED_CHAT_ORIGINS:+$CORS_ALLOWED_CHAT_ORIGINS,}http://localhost:${_P_FRONTEND},http://127.0.0.1:${_P_FRONTEND}"
fi
_DB_CONSOLE="$(slot_db_name "$_SLOT" console)"; _DB_DOCSTORE="$(slot_db_name "$_SLOT" docstore)"
_DB_STATE="$(slot_db_state "$_SLOT")"
if [[ "$_SLOT" != 0 ]]; then
  # Uploads belong to the databases that reference them, not to the claim: `down --keep-db`
  # releases the claim but keeps both.
  LOCAL_STORAGE_PATH="${LOCAL_STORAGE_PATH:-$_DB_STATE.uploads}"
fi
_FRONTEND_URL="http://localhost:${_P_FRONTEND}"
mkdir -p "$_LOG_DIR"

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

# Read SSM parameters in batches: `_ssm_load VAR=/ssm/name ...` sets each VAR whose parameter SSM
# returns and leaves the others unset. `get-parameters` takes up to ten names per call; one call
# per secret cost ~4 s each, fifteen times on every start. A batch fails as a whole when one of
# its names is denied, so a failed batch is read again one name at a time — one optional secret
# the profile may not read must not cost the required ones.
_ssm_emit() {  # get-parameter(s) JSON on stdin, VAR=/name pairs as arguments
  python3 -c '
import json, shlex, sys
doc = json.load(sys.stdin)
params = doc.get("Parameters") or ([doc["Parameter"]] if "Parameter" in doc else [])
values = {p["Name"]: p["Value"] for p in params}
for pair in sys.argv[1:]:
    var, name = pair.split("=", 1)
    if name in values:
        print(f"{var}={shlex.quote(values[name])}")
' "$@"
}
_ssm_load() {
  local _pairs=("$@") _names=() _p _n _i=0 _out _err
  for _p in "${_pairs[@]}"; do _names+=("${_p#*=}"); done
  _err="$(mktemp)"
  while [[ $_i -lt ${#_names[@]} ]]; do
    if _out=$(aws ssm get-parameters --names "${_names[@]:$_i:10}" --with-decryption --output json 2>"$_err"); then
      eval "$(printf '%s' "$_out" | _ssm_emit "${_pairs[@]}")"
    else
      warn "Batched SSM read failed ($(head -1 "$_err")); reading those secrets one by one"
      for _n in "${_names[@]:$_i:10}"; do
        _out=$(aws ssm get-parameter --name "$_n" --with-decryption --output json 2>/dev/null) || continue
        eval "$(printf '%s' "$_out" | _ssm_emit "${_pairs[@]}")"
      done
    fi
    _i=$((_i + 10))
  done
  rm -f "$_err"
}

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
printf "${CYAN}│${RESET}    ${GREEN}✓${RESET} PostgreSQL (console)       ${DIM}(Docker, localhost:5401)${RESET}\n"
printf "${CYAN}│${RESET}    ${GREEN}✓${RESET} PostgreSQL (docstore)      ${DIM}(Docker, localhost:5402; also holds checkpoints)${RESET}\n"
printf "${CYAN}│${RESET}    ${GREEN}✓${RESET} Model Gateway   ${DIM}(LiteLLM proxy, Docker, localhost:${LLM_GATEWAY_PORT:-4000}; tab + logs/litellm.log)${RESET}\n"
if [[ "$_OIDC_MODE" == "local" ]]; then
  printf "${CYAN}│${RESET}    ${GREEN}✓${RESET} Keycloak        ${DIM}(Docker, localhost:8180)${RESET}\n"
else
  printf "${CYAN}│${RESET}    ${DIM}✗ Keycloak        (skipped — using remote OIDC)${RESET}\n"
fi
printf "${CYAN}│${RESET}    ${GREEN}✓${RESET} DB migrations   ${DIM}(Rambler, auto-applied)${RESET}\n"
if [[ "$_SLOT" == 0 ]]; then
  printf "${CYAN}│${RESET}    ${GREEN}✓${RESET} Slack (FE+BE)   ${DIM}(Docker)${RESET}\n"
  printf "${CYAN}│${RESET}    ${GREEN}✓${RESET} Google Chat     ${DIM}(Node.js)${RESET}\n"
else
  printf "${CYAN}│${RESET}    ${DIM}✗ Slack, Google Chat (slot 0 only)${RESET}\n"
  printf "${CYAN}│${RESET}    ${GREEN}✓${RESET} Slot $_SLOT          ${DIM}(ports 4${_SLOT}000-4${_SLOT}999, databases $_DB_CONSOLE/$_DB_DOCSTORE)${RESET}\n"
fi

printf "${CYAN}│${RESET}                                                        ${CYAN}│${RESET}\n"

# Optional
# Debugging
if [[ -n "$_DEBUG_MODE" ]]; then
  printf "${CYAN}│${RESET}  Debugging:                                            ${CYAN}│${RESET}\n"
  printf "${CYAN}│${RESET}    ${GREEN}✓${RESET} debugpy enabled ${DIM}(attach via VS Code launch.json)${RESET}\n"
  printf "${CYAN}│${RESET}    ${DIM}  backend=${_P_DBG_BACKEND} orchestrator=${_P_DBG_ORCHESTRATOR}${RESET}\n"
  printf "${CYAN}│${RESET}    ${DIM}  runner=${_P_DBG_RUNNER} voice-agent=${_P_DBG_VOICE}${RESET}\n"
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
if [[ -n "$_HEADLESS" ]]; then
  _confirm=Y
else
  printf "${CYAN}▸ Proceed? [Y/n] ${RESET}"
  read -r _confirm
fi
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
AZURE_API_BASE="${AZURE_API_BASE:-}"
AZURE_AI_API_KEY="${AZURE_AI_API_KEY:-}"
AZURE_AI_API_BASE="${AZURE_AI_API_BASE:-}"
AWS_BEDROCK_REGION="${AWS_BEDROCK_REGION:-}"
GCP_KEY="${GCP_KEY:-}"
GCP_PROJECT_ID="${GCP_PROJECT_ID:-}"
GCP_LOCATION="${GCP_LOCATION:-}"
CHECKPOINT_S3_BUCKET_NAME="${CHECKPOINT_S3_BUCKET_NAME:-}"
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

  _SSM_PAIRS=(
    _SSM_AZURE=/nannos/azure-ai-api-key
    _SSM_GCP=/nannos/infrastructure-agents/gcp-key
    _SSM_LANGSMITH=/nannos/infrastructure-agents/langsmith-api-key
    _SSM_GOAUTH_ID=/nannos/infrastructure-agents/google-oauth-client-id
    _SSM_GOAUTH_SECRET=/nannos/infrastructure-agents/google-oauth-client-secret
    _SSM_TWILIO_SID=/nannos/twilio/account-sid
    _SSM_TWILIO_KEY=/nannos/twilio/api-key
    _SSM_TWILIO_SECRET=/nannos/twilio/api-secret
    _SSM_TWILIO_VSID=/nannos/twilio/verify-service-sid
    _SSM_TWILIO_VKEY=/nannos/twilio/verify-api-key
    _SSM_TWILIO_VSECRET=/nannos/twilio/verify-api-secret
  )
  # The remote-OIDC client secrets ride in the same batch (read in "OIDC configuration" below).
  if [[ "$_OIDC_MODE" == "remote-ssm" ]]; then
    _SSM_PAIRS+=(
      _SSM_KC_CONSOLE=/nannos/keycloak/agent-console-client-secret
      _SSM_KC_ORCHESTRATOR=/nannos/keycloak/orchestrator-secret
      _SSM_KC_ADMIN=/nannos/keycloak/nannos-admin-secret
      _SSM_KC_RUNNER=/nannos/keycloak/agent-runner-secret
    )
  fi
  _ssm_load "${_SSM_PAIRS[@]}"

  # One Azure resource: the Nannos AI Foundry one. A key already set (e.g. in .env) wins.
  if [[ -z "$AZURE_AI_API_KEY" && -z "$AZURE_OPENAI_API_KEY" ]]; then
    if [[ -n "${_SSM_AZURE:-}" ]]; then
      AZURE_AI_API_KEY="$_SSM_AZURE"
    else
      warn "Could not fetch /nannos/azure-ai-api-key from SSM — Azure models disabled"
    fi
  fi

  if [[ -n "${_SSM_GCP:-}" ]]; then
    GCP_KEY="$_SSM_GCP"
    GCP_PROJECT_ID="rcplus-alloy-gcp"
    GCP_LOCATION="global"
    ok "GCP Vertex AI configured"
  else
    warn "Could not fetch GCP_KEY from SSM — Vertex AI disabled"
  fi

  if [[ -z "${LANGSMITH_API_KEY:-}" ]]; then
    if [[ -n "${_SSM_LANGSMITH:-}" ]]; then
      LANGSMITH_API_KEY="$_SSM_LANGSMITH"
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

  CHECKPOINT_S3_BUCKET_NAME="dev-nannos-infrastructure-agents-orchestrator-checkpoints"
  DOCUMENT_STORE_S3_BUCKET="dev-nannos-infrastructure-agents-files"
  FILES_S3_BUCKET="dev-nannos-infrastructure-agents-files"
  CATALOG_VECTOR_BUCKET_NAME="dev-nannos-infrastructure-agents-catalog-vectors"
  CATALOG_THUMBNAILS_S3_BUCKET="dev-nannos-infrastructure-agents-catalog-thumbnails"

  # Google OAuth for catalog Drive sync (optional)
  if [[ -n "${_SSM_GOAUTH_ID:-}" ]]; then
    GOOGLE_OAUTH_CLIENT_ID="$_SSM_GOAUTH_ID"
    ok "Google OAuth client ID loaded from SSM"
  else
    warn "Could not fetch Google OAuth client ID from SSM — catalog Drive sync disabled"
  fi
  if [[ -n "${_SSM_GOAUTH_SECRET:-}" ]]; then
    GOOGLE_OAUTH_CLIENT_SECRET="$_SSM_GOAUTH_SECRET"
    ok "Google OAuth client secret loaded from SSM"
  else
    warn "Could not fetch Google OAuth client secret from SSM"
  fi

  # Twilio credentials (optional — needed for voice agent + phone verification)
  if [[ -n "${_SSM_TWILIO_SID:-}" ]]; then
    TWILIO_ACCOUNT_SID="$_SSM_TWILIO_SID"
    ok "Twilio Account SID loaded from SSM"
  else
    warn "Could not fetch Twilio Account SID from SSM — voice calls and phone verification disabled"
  fi
  TWILIO_API_KEY="${_SSM_TWILIO_KEY:-$TWILIO_API_KEY}"
  TWILIO_API_SECRET="${_SSM_TWILIO_SECRET:-$TWILIO_API_SECRET}"
  TWILIO_VERIFY_SERVICE_SID="${_SSM_TWILIO_VSID:-$TWILIO_VERIFY_SERVICE_SID}"
  TWILIO_VERIFY_API_KEY="${_SSM_TWILIO_VKEY:-$TWILIO_VERIFY_API_KEY}"
  TWILIO_VERIFY_API_SECRET="${_SSM_TWILIO_VSECRET:-$TWILIO_VERIFY_API_SECRET}"

  ok "AWS resources configured (dev environment)"
fi

# Azure: the Nannos AI Foundry resource serves both routes with one key. `azure/<deployment>`
# reads AZURE_API_BASE + AZURE_OPENAI_API_KEY; `azure_ai/<model>` reads AZURE_AI_API_BASE +
# AZURE_AI_API_KEY. AZURE_API_BASE must be the bare host: LiteLLM appends /openai/... itself,
# so a base ending in /openai/v1 doubles the path and every call 404s.
AZURE_AI_API_KEY="${AZURE_AI_API_KEY:-$AZURE_OPENAI_API_KEY}"
AZURE_OPENAI_API_KEY="${AZURE_OPENAI_API_KEY:-$AZURE_AI_API_KEY}"
if [[ -n "$AZURE_AI_API_KEY" ]]; then
  AZURE_API_BASE="${AZURE_API_BASE:-https://nannos-resource.openai.azure.com}"
  AZURE_API_BASE="${AZURE_API_BASE%/}"; AZURE_API_BASE="${AZURE_API_BASE%/openai/v1}"
  AZURE_AI_API_BASE="${AZURE_AI_API_BASE:-https://nannos-resource.services.ai.azure.com}"
  ok "Azure (Nannos AI Foundry) configured"
fi

# ── OIDC configuration ──
_OIDC_ISSUER="http://localhost:8180/realms/nannos"
_OIDC_SECRET_BACKEND="local-secret"
_OIDC_SECRET_ORCHESTRATOR="local-secret"
_OIDC_SECRET_ADMIN="local-secret"
_OIDC_SECRET_AGENT_RUNNER="local-secret"
_KC_BASE_URL="http://localhost:8180"
_KC_REALM="nannos"

if [[ "$_OIDC_MODE" == "remote-ssm" ]]; then
  _OIDC_ISSUER="$OIDC_ISSUER"
  _KC_BASE_URL="${OIDC_ISSUER%/realms/*}"
  _KC_REALM="${OIDC_ISSUER##*/realms/}"

  [[ -n "${_SSM_KC_CONSOLE:-}" ]] || err "Failed to fetch agent-console OIDC secret from SSM"
  _OIDC_SECRET_BACKEND="$_SSM_KC_CONSOLE"
  [[ -n "${_SSM_KC_ORCHESTRATOR:-}" ]] || err "Failed to fetch orchestrator OIDC secret from SSM"
  _OIDC_SECRET_ORCHESTRATOR="$_SSM_KC_ORCHESTRATOR"
  _OIDC_SECRET_ADMIN="${_SSM_KC_ADMIN:-}"
  [[ -n "$_OIDC_SECRET_ADMIN" ]] || warn "Could not fetch nannos-admin secret — Keycloak group sync disabled"
  _OIDC_SECRET_AGENT_RUNNER="${_SSM_KC_RUNNER:-}"
  [[ -n "$_OIDC_SECRET_AGENT_RUNNER" ]] || warn "Could not fetch agent-runner secret — agent runner will use backend secret"

  ok "OIDC secrets loaded from SSM"

elif [[ "$_OIDC_MODE" == "remote-manual" ]]; then
  _OIDC_ISSUER="$OIDC_ISSUER"
  _KC_BASE_URL="${OIDC_ISSUER%/realms/*}"
  _KC_REALM="${OIDC_ISSUER##*/realms/}"

  [[ -z "$_HEADLESS" ]] || err "Remote OIDC without AWS_PROFILE prompts for a secret; a headless slot cannot"
  printf "${CYAN}▸ Enter OIDC client secret (shared for all services): ${RESET}"
  read -r _shared_secret
  if [[ -z "$_shared_secret" ]]; then
    err "OIDC client secret is required when using external OIDC without AWS_PROFILE"
  fi
  _OIDC_SECRET_BACKEND="$_shared_secret"
  _OIDC_SECRET_ORCHESTRATOR="$_shared_secret"
  _OIDC_SECRET_ADMIN="$_shared_secret"
  _OIDC_SECRET_AGENT_RUNNER="$_shared_secret"
  ok "OIDC configured with shared secret"
fi

# ─── 3b. Configure local storage (when not using S3) ─────────────

if [[ -z "$FILES_S3_BUCKET" ]]; then
  log "Configuring local file storage (no S3 bucket configured)..."
  OBJECT_STORAGE_TYPE="local"
  LOCAL_STORAGE_BASE_URL="http://localhost:${CONSOLE_BACKEND_PORT}/api/v1/files/download"
  LOCAL_STORAGE_PATH="${LOCAL_STORAGE_PATH:-./local-uploads}"
  ok "Local storage configured at: $LOCAL_STORAGE_PATH"
  ok "Download URL: $LOCAL_STORAGE_BASE_URL"
else
  OBJECT_STORAGE_TYPE="s3"
  LOCAL_STORAGE_BASE_URL=""
  LOCAL_STORAGE_PATH=""
fi

# ─── 4. Start infrastructure (PostgreSQL + Keycloak) ──────────────

log "Starting infrastructure..."

cd "$LOCAL_DEV_DIR"
# One realm path for every checkout, so the compose config (and Keycloak) is the same from any
# worktree. Keycloak imports it only into an empty realm; a running Keycloak is never re-created.
export NANNOS_KEYCLOAK_REALM_FILE="${NANNOS_HOME:-$HOME/.nannos}/keycloak/realm-export.json"
mkdir -p "$(dirname "$NANNOS_KEYCLOAK_REALM_FILE")"
cmp -s keycloak/realm-export.json "$NANNOS_KEYCLOAK_REALM_FILE" || cp keycloak/realm-export.json "$NANNOS_KEYCLOAK_REALM_FILE"
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

# ─── 4b. Slot databases ──────────────────────────────────────────
# A slot's databases are created on its first start, empty or (--from-slot0) as a copy of slot
# 0's, and kept until `just down` drops them. They belong to the worktree that created them.

_db_exists() { docker exec "$1" psql -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$2'" </dev/null | grep -q 1; }
_hash_mismatches() {  # recorded-hashes-file: the files whose current hash differs from the record
  slot_migration_hashes "$ROOT_DIR" \
    | awk 'NR == FNR { seen[$1 " " $3] = $2; next }
           ($1 " " $3) in seen && seen[$1 " " $3] != "unknown" && seen[$1 " " $3] != $2 { print $1 "/" $3 }' "$1" -
}

if [[ "$_SLOT" != 0 ]]; then
  _COPY_NOW=""
  if _db_exists "$PG_CONSOLE_CONTAINER" "$_DB_CONSOLE"; then
    _OWNER="$(cat "$_DB_STATE.owner" 2>/dev/null || true)"
    if [[ -n "$_OWNER" && "$_OWNER" != "$ROOT_DIR" ]]; then
      err "Slot $_SLOT's kept databases belong to $_OWNER, not this worktree. 'just down $_SLOT' drops them."
    fi
    [[ -z "$_FROM_SLOT0" ]] || warn "Slot $_SLOT already has databases (kept); --from-slot0 copies only into new ones ('just db-reset $_SLOT' starts over)"
  elif [[ -n "$_FROM_SLOT0" ]]; then
    # Slot 0 runs whichever branch started it last. Copy only a schema this worktree accounts
    # for: no migration this worktree lacks, and none applied from other contents than its own.
    for _spec in $SLOT_DB_SPECS; do
      IFS=: read -r _logical _container _ddl <<< "$_spec"
      _db_exists "$_container" "$_logical" || err "Slot 0 has no '$_logical' database to copy (run 'just start-local' once)"
      _AHEAD=$(comm -23 \
        <(docker exec "$_container" psql -U postgres -d "$_logical" -tAc "SELECT migration FROM migrations" </dev/null | sort) \
        <(ls "$ROOT_DIR/$_ddl" | sort))
      if [[ -n "$_AHEAD" ]]; then
        err "Slot 0's $_logical has migrations this worktree does not: $(echo $_AHEAD). Slot 0 runs another branch; start without --from-slot0."
      fi
    done
    _S0_HASHES="$(slot_db_state 0).sha256"
    if [[ -f "$_S0_HASHES" ]]; then
      _DIFFERENT="$(_hash_mismatches "$_S0_HASHES")"
      if [[ -n "$_DIFFERENT" ]]; then
        err "Slot 0 applied other contents of: $(echo $_DIFFERENT). Slot 0 runs another branch; start without --from-slot0."
      fi
      _UNKNOWN="$(awk '$2 == "unknown"' "$_S0_HASHES" | wc -l | tr -d ' ')"
      if [[ "$_UNKNOWN" != 0 ]]; then
        warn "Slot 0 applied $_UNKNOWN migrations before it kept a record of their contents; the copy assumes those are this worktree's"
      fi
    else
      warn "Slot 0 has no record of the migration contents it applied (it records them from its next 'just start-local'); the copy assumes they are this worktree's"
    fi
    _COPY_NOW=1
  fi

  _CREATED=""
  for _spec in $SLOT_DB_SPECS; do
    IFS=: read -r _logical _container _ddl <<< "$_spec"
    _db="$(slot_db_name "$_SLOT" "$_logical")"
    _db_exists "$_container" "$_db" && continue
    docker exec "$_container" psql -U postgres -qc "CREATE DATABASE \"$_db\"" </dev/null >/dev/null
    _CREATED=1
    if [[ -n "$_COPY_NOW" ]]; then
      log "Copying slot 0's $_logical into $_db..."
      # -Z0: the dump is restored in the same pipe, compressing it only costs CPU.
      if ! docker exec "$_container" bash -c "set -o pipefail; pg_dump -U postgres -Fc -Z0 --no-owner --no-privileges '$_logical' | pg_restore -U postgres --no-owner --no-privileges --exit-on-error -d '$_db'" </dev/null; then
        slot_drop_databases "$_SLOT" || true
        err "Copying slot 0's $_logical failed; slot $_SLOT's databases were dropped again"
      fi
      ok "Copied slot 0's $_logical into $_db"
    else
      ok "Created database $_db"
    fi
  done
  # New databases are this worktree's, whatever an earlier owner file says (databases dropped
  # outside `just down` leave it behind); kept ones from before owners were recorded become so.
  if [[ -n "$_CREATED" || ! -f "$_DB_STATE.owner" ]]; then
    echo "$ROOT_DIR" > "$_DB_STATE.owner"
  fi

  if [[ -n "$_COPY_NOW" ]]; then
    # A copy carries slot 0's schedule with it. Run beside slot 0, each job would fire twice and
    # notify the same people, so the copy suspends every job (visibly, with a reason) and drops
    # the work slot 0 still had in flight: owed notices, pending retries, runs mid-execution and
    # queued catalog syncs. It also carries slot 0's outbound SCIM endpoints, which every user or
    # group change would push to: they are disabled (their tokens blanked as well, so re-enabling
    # one in the copy cannot reach slot 0's downstream system either). With no enabled MCP-gateway
    # endpoint, the copy cannot manage MCP server access for groups. Its delivery channels point
    # at slot 0's channel clients, which would message real users once a job is unsuspended:
    # their webhooks go to a discard address and their secrets are blanked.
    docker exec -i "$PG_CONSOLE_CONTAINER" psql -U postgres -d "$_DB_CONSOLE" -v ON_ERROR_STOP=1 -q >/dev/null <<SQL || { slot_drop_databases "$_SLOT" || true; err "Could not neutralize the copy; slot $_SLOT's databases were dropped again"; }
BEGIN;
UPDATE scheduled_job_definitions
   SET suspended_at = now(), suspended_by_user_id = NULL, updated_at = now(),
       suspended_reason = 'Copied from slot 0 into local stack slot $_SLOT. Unsuspend it to run it here, beside slot 0.'
 WHERE deleted_at IS NULL AND suspended_at IS NULL;
UPDATE scheduled_job_subscriptions SET retry_at = NULL WHERE retry_at IS NOT NULL;
UPDATE scheduled_job_runs SET status = 'interrupted', completed_at = COALESCE(completed_at, now()) WHERE status = 'running';
UPDATE scheduled_job_runs SET notice_due_at = NULL WHERE notice_due_at IS NOT NULL;
UPDATE outbound_scim_endpoints SET enabled = false, bearer_token = '', updated_at = now() WHERE deleted_at IS NULL;
UPDATE delivery_channels
   SET webhook_url = 'http://127.0.0.1:9/delivery-disabled-in-slot-copy', secret = '', updated_at = now();
UPDATE catalog_sync_jobs
   SET status = 'cancelled', completed_at = COALESCE(completed_at, now()),
       error_details = jsonb_build_object('reason', 'Copied from slot 0: cancelled in slot $_SLOT')
 WHERE status IN ('pending', 'running', 'reindexing', 'paused', 'cancelling');
COMMIT;
SQL
    touch "$_DB_STATE.from-slot0"
    ok "Copy neutralized: scheduled jobs suspended, slot 0's in-flight runs and notices dropped"
  fi
fi

# Rambler records which migrations ran, not what they contained. A slot's databases can outlive
# a migration edit (`just down --keep-db`), so a slot keeps the hash of every migration file it
# applied and refuses to start when one has since changed: re-applying an edited migration is
# not something Rambler does, and the schema would silently drift.
if [[ "$_SLOT" != 0 && -f "$_DB_STATE.sha256" ]]; then
  _EDITED="$(_hash_mismatches "$_DB_STATE.sha256")"
  if [[ -n "$_EDITED" ]]; then
    err "Migrations edited since slot $_SLOT applied them: $(echo $_EDITED). Run 'just db-reset $_SLOT'."
  fi
fi

# ─── 5. Run database migrations ──────────────────────────────────

# Slot 0 starts keeping a record of migration contents with this version of the script. What it
# applied before then is unknown: recorded as such, never certified with today's files.
_PRE_APPLIED=""
if [[ "$_SLOT" == 0 && ! -f "$_DB_STATE.sha256" ]]; then
  _PRE_APPLIED="$(mktemp)"
  for _spec in $SLOT_DB_SPECS; do
    IFS=: read -r _logical _container _ddl <<< "$_spec"
    { docker exec "$_container" psql -U postgres -d "$_logical" -tAc "SELECT migration FROM migrations" </dev/null 2>/dev/null || true; } \
      | sed "s|^|$_logical |" >> "$_PRE_APPLIED"
  done
fi

log "Running database migrations (Rambler)..."

CONSOLE_MIGRATIONS_IMAGE="nannos-console-migrations:local"
DOCSTORE_MIGRATIONS_IMAGE="nannos-docstore-migrations:local"

# Build both migration images with the current context's own docker-driver builder, whatever
# buildx builder is the default. A docker-container builder (needed for multi-platform pushes)
# exports and re-loads the whole image on every build, even a full cache hit: ~70 s vs ~7 s.
# These images are local-only; release builds keep the default builder.
# Docker without buildx (or with BuildKit off) has no --builder flag: build as before there.
_BUILDER_ARGS=()
_CONTEXT="$(docker context show 2>/dev/null || true)"
if [[ -n "$_CONTEXT" ]] && docker buildx inspect "$_CONTEXT" >/dev/null 2>&1; then
  _BUILDER_ARGS=(--builder "$_CONTEXT")
fi
docker build ${_BUILDER_ARGS[@]+"${_BUILDER_ARGS[@]}"} -t "$CONSOLE_MIGRATIONS_IMAGE" "$ROOT_DIR/packages/console-backend/sqlmigrations" --quiet
docker build ${_BUILDER_ARGS[@]+"${_BUILDER_ARGS[@]}"} -t "$DOCSTORE_MIGRATIONS_IMAGE" "$ROOT_DIR/packages/orchestrator-agent/sqlmigrations" --quiet

# Install pgvector extension on the docstore database
docker run --rm \
  --network host \
  --user 0 \
  --entrypoint psql \
  -e PGPASSWORD=password \
  "$DOCSTORE_MIGRATIONS_IMAGE" "postgresql://postgres:password@127.0.0.1:5402/$_DB_DOCSTORE" -c "
    CREATE EXTENSION IF NOT EXISTS vector;
  "

# Run console-backend migrations (public schema on port 5401)
log "Applying console-backend migrations (db: $_DB_CONSOLE, port: 5401)..."
docker run --rm \
  --network host \
  --user 0 \
  -e PGHOST=127.0.0.1 \
  -e PGPORT=5401 \
  -e PGUSER=postgres \
  -e PGPASSWORD=password \
  -e PGDATABASE="$_DB_CONSOLE" \
  -e PGSCHEMA=public \
  -e RAMBLER_SSLMODE=disable \
  "$CONSOLE_MIGRATIONS_IMAGE"

# Run orchestrator-agent migrations (public schema on port 5402)
log "Applying orchestrator-agent migrations (db: $_DB_DOCSTORE, port: 5402)..."
docker run --rm \
  --network host \
  --user 0 \
  -e PGHOST=127.0.0.1 \
  -e PGPORT=5402 \
  -e PGUSER=postgres \
  -e PGPASSWORD=password \
  -e PGDATABASE="$_DB_DOCSTORE" \
  -e PGSCHEMA=public \
  -e RAMBLER_SSLMODE=disable \
  "$DOCSTORE_MIGRATIONS_IMAGE"

ok "Database migrations applied"
mkdir -p "$NANNOS_SLOTS_DIR"
if [[ "$_SLOT" == 0 ]]; then
  # Slot 0 keeps the contents it first applied each migration with — what a --from-slot0 copy
  # checks a worktree against. A file already recorded keeps its first hash.
  if [[ -n "$_PRE_APPLIED" ]]; then
    slot_migration_hashes "$ROOT_DIR" \
      | awk 'NR == FNR { pre[$0]; next } { print $1, (($1 " " $3) in pre ? "unknown" : $2) "  " $3 }' "$_PRE_APPLIED" - \
      > "$_DB_STATE.sha256"
    rm -f "$_PRE_APPLIED"
  else
    # A file already recorded keeps its first hash.
    { cat "$_DB_STATE.sha256" 2>/dev/null || true; slot_migration_hashes "$ROOT_DIR"; } \
      | awk '!seen[$1 " " $3]++' > "$_DB_STATE.sha256.tmp" && mv "$_DB_STATE.sha256.tmp" "$_DB_STATE.sha256"
  fi
else
  slot_migration_hashes "$ROOT_DIR" > "$_DB_STATE.sha256"
fi

# Seed: make all users administrators for local dev
docker compose exec -T postgres-console psql -U postgres -d "$_DB_CONSOLE" -c \
  "UPDATE users SET is_administrator = true WHERE is_administrator = false;" \
  >/dev/null 2>&1 || true

# Seed: ensure test@local.dev user exists as admin (for first-time local dev)
docker compose exec -T postgres-console psql -U postgres -d "$_DB_CONSOLE" -c \
  "INSERT INTO users (id, sub, email, first_name, last_name, is_administrator, role, status, created_at, updated_at)
   VALUES (gen_random_uuid(), 'local-test-user', 'test@local.dev', 'Test', 'User', true, 'admin', 'active', now(), now())
   ON CONFLICT (LOWER(email)) WHERE deleted_at IS NULL DO UPDATE SET is_administrator = true, role = 'admin';" \
  >/dev/null 2>&1 || true

# ─── 6. Wait for Keycloak ────────────────────────────────────────

if [[ "$_OIDC_MODE" == "local" ]]; then

log "Waiting for Keycloak..."
KEYCLOAK_RETRIES=0
until curl -sf http://localhost:8180/realms/nannos/.well-known/openid-configuration >/dev/null 2>&1; do
  KEYCLOAK_RETRIES=$((KEYCLOAK_RETRIES + 1))
  if [[ $KEYCLOAK_RETRIES -ge 240 ]]; then
    err "Keycloak did not become ready after 240s. Check: docker compose logs keycloak"
  fi
  sleep 1
done
ok "Keycloak is ready (realm: nannos)"

# ─── 6b. Fix Keycloak client secrets ─────────────────────────────
# Keycloak ignores the "secret" field in realm exports and generates random ones.
# We force all confidential clients to use "local-secret" via the Admin API.

log "Configuring Keycloak client secrets..."

# Keycloak 26.x defaults the master realm to sslRequired!=NONE, blocking HTTP
# admin token requests from the host. Disable it via kcadm.sh (internal HTTP).
docker compose exec -T keycloak /opt/keycloak/bin/kcadm.sh update realms/master \
  -s sslRequired=NONE \
  --server http://localhost:8080 --realm master --user admin --password admin \
  >/dev/null 2>&1 || warn "Could not disable master realm SSL (may already be NONE)"

KC_ADMIN_TOKEN=""
for i in $(seq 1 15); do
  KC_ADMIN_TOKEN=$(curl -s -X POST http://localhost:8180/realms/master/protocol/openid-connect/token \
    -d "grant_type=password" \
    -d "client_id=admin-cli" \
    -d "username=admin" \
    -d "password=admin" \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('access_token',''))" || true)
  [[ -n "$KC_ADMIN_TOKEN" ]] && break
  sleep 2
done

if [[ -z "$KC_ADMIN_TOKEN" ]]; then
  err "Failed to obtain Keycloak admin token after retries. Check: docker compose logs keycloak"
fi

for CLIENT_ID in agent-console orchestrator nannos-admin; do
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

# ─── 6c. Grant nannos-admin service account realm-management roles ──
# The realm export doesn't include service account role mappings, so we
# assign them here to allow the backend to manage groups/users in Keycloak.

log "Granting nannos-admin service account roles..."

NANNOS_ADMIN_UUID=$(curl -sf -H "Authorization: Bearer $KC_ADMIN_TOKEN" \
  "http://localhost:8180/admin/realms/nannos/clients?clientId=nannos-admin" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")

# Get the service account user for nannos-admin
SA_USER_ID=$(curl -sf -H "Authorization: Bearer $KC_ADMIN_TOKEN" \
  "http://localhost:8180/admin/realms/nannos/clients/$NANNOS_ADMIN_UUID/service-account-user" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# Get the realm-management client UUID
REALM_MGMT_UUID=$(curl -sf -H "Authorization: Bearer $KC_ADMIN_TOKEN" \
  "http://localhost:8180/admin/realms/nannos/clients?clientId=realm-management" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")

# Get available realm-management roles and assign the needed ones
ROLES_JSON=$(curl -sf -H "Authorization: Bearer $KC_ADMIN_TOKEN" \
  "http://localhost:8180/admin/realms/nannos/clients/$REALM_MGMT_UUID/roles")

ROLES_TO_ASSIGN=$(echo "$ROLES_JSON" | python3 -c "
import sys, json
roles = json.load(sys.stdin)
needed = ['manage-users', 'view-users', 'query-groups', 'query-users']
selected = [r for r in roles if r['name'] in needed]
print(json.dumps(selected))
")

curl -sf -X POST -H "Authorization: Bearer $KC_ADMIN_TOKEN" -H "Content-Type: application/json" \
  "http://localhost:8180/admin/realms/nannos/users/$SA_USER_ID/role-mappings/clients/$REALM_MGMT_UUID" \
  -d "$ROLES_TO_ASSIGN" >/dev/null

ok "nannos-admin service account roles granted"

# ─── 6d. Register this slot's URIs on the local realm ────────────
# The realm export allows slot 0's ports only. A slot adds its own backend and frontend to the
# agent-console client — read-modify-write, so it re-reads and retries in case another slot
# was registering at the same moment. (Against a remote IdP the slot URIs are registered with
# that IdP's client provisioning instead; see ADR-0016.)
if [[ "$_SLOT" != 0 ]]; then
  log "Registering slot $_SLOT on the local realm..."
  _AC_UUID=$(curl -sf -H "Authorization: Bearer $KC_ADMIN_TOKEN" \
    "http://localhost:8180/admin/realms/nannos/clients?clientId=agent-console" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")
  # Two slots registering at once would each PUT the list it read, dropping the other's URIs.
  slot_lock keycloak-realm 9 || err "Could not take the local realm lock"
  _SLOT_URIS_OK=""
  for _try in 1 2 3; do
    _AC_PATCH=$(curl -sf -H "Authorization: Bearer $KC_ADMIN_TOKEN" \
      "http://localhost:8180/admin/realms/nannos/clients/$_AC_UUID" | python3 -c '
import json, sys
client = json.load(sys.stdin)
origins = sys.argv[1:]
patterns = [f"{o}/*" for o in origins]
uris = client.get("redirectUris") or []
web = client.get("webOrigins") or []
attrs = client.get("attributes") or {}
logout = [u for u in attrs.get("post.logout.redirect.uris", "").split("##") if u]
missing = [u for u in patterns if u not in uris] + [o for o in origins if o not in web] + [u for u in patterns if u not in logout]
if not missing:
    print("")
    sys.exit()
attrs["post.logout.redirect.uris"] = "##".join(logout + [u for u in patterns if u not in logout])
print(json.dumps({
    "redirectUris": uris + [u for u in patterns if u not in uris],
    "webOrigins": web + [o for o in origins if o not in web],
    "attributes": attrs,
}))
' "http://localhost:${CONSOLE_BACKEND_PORT}" "$_FRONTEND_URL")
    if [[ -z "$_AC_PATCH" ]]; then
      _SLOT_URIS_OK=1
      break
    fi
    curl -sf -X PUT -H "Authorization: Bearer $KC_ADMIN_TOKEN" -H "Content-Type: application/json" \
      "http://localhost:8180/admin/realms/nannos/clients/$_AC_UUID" -d "$_AC_PATCH" >/dev/null || true
  done
  slot_unlock 9
  [[ -n "$_SLOT_URIS_OK" ]] || err "Could not register slot $_SLOT's URIs on the local realm's agent-console client"
  ok "Slot $_SLOT registered on the local realm (localhost:${CONSOLE_BACKEND_PORT}, localhost:${_P_FRONTEND})"
fi

else
  log "Skipping local Keycloak (using external OIDC)"
fi  # _OIDC_MODE

# ─── 7. Install dependencies ─────────────────────────────────────

log "Installing Python dependencies..."
cd "$ROOT_DIR"

# Sync all Python packages in parallel
for pkg in orchestrator-agent agent-runner console-backend soffice-worker; do
  (cd "packages/$pkg" && uv sync --quiet) &
done
(cd "packages/voice-agent" && uv sync --quiet) &
wait
ok "Python dependencies installed"

log "Installing frontend dependencies..."
cd "$ROOT_DIR/packages/console-frontend"
npm install --silent 2>/dev/null
ok "Frontend dependencies installed"

# @nannos/embed-sdk is a workspace package whose exports point at dist/. The dev
# server reads the SDK's SOURCE (serve-only aliases in the console's
# vite.config.ts), but tsconfig has no paths mapping — so the IDE and `tsc -b`
# still typecheck against dist/*.d.ts, and `npm run build` still bundles dist.
# Build once here so both are valid; the "embed-sdk" mprocs process then keeps
# only the .d.ts and the stylesheet fresh (see the note there).
log "Building embed-sdk..."
(cd "$ROOT_DIR/packages/embed-sdk" && npm run build --silent >/dev/null 2>&1) \
  || err "embed-sdk build failed — run 'npm run build' in packages/embed-sdk"
ok "embed-sdk built"

# ─── 7b. Local Model Gateway (LiteLLM proxy) ───────────────────────
# Gateway-only architecture: all LLM calls route through the proxy,
# there is no per-provider fallback. Auto-launch a local proxy fronting whatever
# creds / local LLM are available, and point the services at it.
LLM_GATEWAY_PORT="${LLM_GATEWAY_PORT:-4000}"
export LLM_GATEWAY_URL="http://localhost:${LLM_GATEWAY_PORT}"
export LLM_GATEWAY_API_KEY="sk-nannos-local"
# Master key for the proxy management API (/model/*). Locally it equals the app key;
# in real envs they differ (master key only on proxy + console-backend).
# Exported so console-backend (launched via mprocs below) can register/list models.
export LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY:-sk-nannos-local}"
# Shared secret for proxy → console-backend cost ingestion. Exported so console-backend
# (launched via mprocs below) accepts it on /api/v1/usage/gateway-batch-log.
export GATEWAY_INGEST_TOKEN="${GATEWAY_INGEST_TOKEN:-sk-nannos-local-ingest}"
# Pinned to match the prod base image (packages/litellm-proxy/Dockerfile) so local
# reproduces prod's Vertex region-resolution behavior. Override with LITELLM_IMAGE.
_LITELLM_IMAGE="${LITELLM_IMAGE:-ghcr.io/berriai/litellm:v1.103.0@sha256:bd089afdcd35b894b14a93f9743cdc8b591f82da1a38dd43a010a7b0c9de5fd7}"

log "Starting local Model Gateway (LiteLLM proxy) on :${LLM_GATEWAY_PORT}..."

# Local gateway DB: reuse the console Postgres with a dedicated `litellm` schema
# (mirrors the prod shared-RDS pattern). store_model_in_db lets the console register
# models at runtime — without it /model/new returns "No DB Connected". The container
# reaches the host Postgres (published on :5401) via host.docker.internal.
export LITELLM_DATABASE_URL="${LITELLM_DATABASE_URL:-postgresql://postgres:password@host.docker.internal:5401/${_DB_CONSOLE}?schema=litellm}"
# Run from $LOCAL_DEV_DIR — that's where the compose project lives (cwd has since
# moved to a package dir, so `docker compose` must be pointed back at it).
( cd "$LOCAL_DEV_DIR" && docker compose exec -T postgres-console psql -U postgres -d "$_DB_CONSOLE" \
  -c "CREATE SCHEMA IF NOT EXISTS litellm;" ) >/dev/null 2>&1 \
  && ok "Gateway DB schema 'litellm' ready (console Postgres)" \
  || warn "Could not pre-create the litellm schema; the proxy will attempt it on boot"

# Resolve a path to its physical location (symlinks expanded).
#
# On macOS /tmp is a symlink to /private/tmp. Docker Desktop shares the physical
# path, so bind-mounting a /tmp/... path can silently create an empty DIRECTORY
# inside the VM instead of mounting the file. The proxy then dies with
# "IsADirectoryError: '/etc/litellm/config.yaml'", and the GCP service-account
# mount below fails *silently* — GOOGLE_APPLICATION_CREDENTIALS ends up pointing
# at a directory, so Vertex auth just doesn't work with nothing in the logs.
#
# `pwd -P` is POSIX and a no-op on Linux, where /tmp is a real directory.
_physical_path() { cd "$(dirname "$1")" && printf '%s/%s\n' "$(pwd -P)" "$(basename "$1")"; }

# Generate the local gateway config on the fly (ephemeral, never committed — the assembled
# config is deployment-specific). Three sources, each owning what only it can own:
#   settings     -> packages/litellm-proxy/litellm-settings.yaml (shared with deployments)
#   general_*    -> inline below (names env vars that differ per environment)
#   model_list   -> $LITELLM_LOCAL_MODELS_FILE when set, else the committed example.
_GW_CONFIG=$(_physical_path "$(mktemp /tmp/nannos-litellm-XXXXXX).yaml")

# Settings come from the committed, SHARED settings file — the same blocks a deployment's
# own config.yaml is expected to carry. Do not inline them here: they used to live in this
# heredoc AND in each deployment's mounted config, kept in step only by comments claiming they
# matched. Nothing verifies such a claim, and a deployment left on num_retries: 0 has no
# retries and no failover at all (nannos#204).
_GW_SETTINGS="$ROOT_DIR/packages/litellm-proxy/litellm-settings.yaml"
if [[ ! -f "$_GW_SETTINGS" ]]; then
  err "Missing gateway settings file: $_GW_SETTINGS"
fi
cat "$_GW_SETTINGS" > "$_GW_CONFIG"

# general_settings stays here: it names env vars that legitimately differ per environment
# (locally LITELLM_DATABASE_URL, in k8s DATABASE_URL), so it cannot be shared verbatim.
cat >> "$_GW_CONFIG" <<'EOF'

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  store_model_in_db: true
  database_url: os.environ/LITELLM_DATABASE_URL
EOF

# model_list: deployment-specific (model ids, regions, provider deployments), so it lives in
# a gitignored YAML file rather than this committed script. Resolution order:
#   1. $LITELLM_LOCAL_MODELS_FILE if set, else ./litellm-local-models.yaml (gitignored)
#   2. ./litellm-local-models.example.yaml (committed template) as a fallback
#   3. empty model_list (register models at runtime via the Model Gateway admin UI)
_LITELLM_MODELS_FILE="${LITELLM_LOCAL_MODELS_FILE:-$ROOT_DIR/litellm-local-models.yaml}"
if [[ -f "$_LITELLM_MODELS_FILE" ]]; then
  cat "$_LITELLM_MODELS_FILE" >> "$_GW_CONFIG"
  ok "Gateway model_list from ${_LITELLM_MODELS_FILE}"
elif [[ -f "$ROOT_DIR/litellm-local-models.example.yaml" ]]; then
  cat "$ROOT_DIR/litellm-local-models.example.yaml" >> "$_GW_CONFIG"
  warn "No litellm-local-models.yaml — using the example template. Copy it and customize:"
  warn "  cp litellm-local-models.example.yaml litellm-local-models.yaml"
else
  printf 'model_list: []\n' >> "$_GW_CONFIG"
  warn "No local model_list — register models at runtime via the Model Gateway admin UI."
fi
if [[ "$_HAS_LOCAL_LLM" == true ]]; then
  # The container reaches the host LLM server via host.docker.internal.
  _GW_LOCAL_BASE="${OPENAI_COMPATIBLE_BASE_URL//localhost/host.docker.internal}"
  _GW_LOCAL_BASE="${_GW_LOCAL_BASE//127.0.0.1/host.docker.internal}"
  _GW_LOCAL_BASE="${_GW_LOCAL_BASE%/}"
  [[ "$_GW_LOCAL_BASE" == */v1 ]] || _GW_LOCAL_BASE="${_GW_LOCAL_BASE}/v1"
  cat >> "$_GW_CONFIG" <<EOF
  - model_name: local
    litellm_params:
      model: openai/${OPENAI_COMPATIBLE_MODEL:-default}
      api_base: ${_GW_LOCAL_BASE}
      api_key: ${OPENAI_COMPATIBLE_API_KEY:-not-needed}
      max_retries: 0
EOF
  ok "Gateway: local model '${OPENAI_COMPATIBLE_MODEL:-default}' → ${_GW_LOCAL_BASE}"
fi

# When using an AWS profile, export its (temporary) credentials so the container
# can reach Bedrock — the SDK profile/SSO chain isn't visible inside the container.
_GW_AWS_ENV=()
if [[ "$_HAS_AWS" == true ]]; then
  if _CREDS=$(aws configure export-credentials --profile "$AWS_PROFILE" --format env 2>/dev/null); then
    eval "$_CREDS"
    _GW_AWS_ENV=(-e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN)
  else
    warn "Could not export AWS credentials for the gateway — Bedrock models may not work locally"
  fi
fi

# Pod-level Vertex auth via ADC, mirroring deployment (k8s projects GCP_KEY to a file and points
# GOOGLE_APPLICATION_CREDENTIALS at it; see the gitops litellm-proxy.yaml). Write the SA JSON to a
# temp file and mount it so google.auth.default() resolves Vertex creds for BOTH config-defined and
# runtime-registered (DB) models — the latter do NOT resolve os.environ/GCP_KEY, exactly as in
# deployment, which is why console registrations must carry no vertex_credentials. Pointing ADC at
# a real file also avoids the GCE-metadata-probe hang you get when only GCP_KEY is set. (The GCP_KEY
# env below stays for config-defined model_list entries that still reference os.environ/GCP_KEY.)
_GW_GCP_ENV=()
if [[ -n "${GCP_KEY:-}" ]]; then
  _GW_GCP_SA=$(_physical_path "$(mktemp /tmp/nannos-litellm-gcp-XXXXXX).json")
  printf '%s' "$GCP_KEY" > "$_GW_GCP_SA"
  _GW_GCP_ENV=(-v "$_GW_GCP_SA:/secrets/gcp/sa.json:ro" -e GOOGLE_APPLICATION_CREDENTIALS=/secrets/gcp/sa.json)
fi

docker rm -f "$_GW_CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$_GW_CONTAINER" \
  -p "${LLM_GATEWAY_PORT}:4000" \
  --add-host=host.docker.internal:host-gateway \
  -v "$_GW_CONFIG:/etc/litellm/config.yaml:ro" \
  -v "$ROOT_DIR/packages/litellm-proxy/custom_logger.py:/etc/litellm/custom_logger.py:ro" \
  -v "$ROOT_DIR/packages/ringier-a2a-sdk/ringier_a2a_sdk/model_capabilities.py:/etc/litellm/nannos_model_capabilities.py:ro" \
  -e PYTHONPATH=/etc/litellm \
  -e LITELLM_MASTER_KEY="$LLM_GATEWAY_API_KEY" \
  -e LITELLM_DATABASE_URL="$LITELLM_DATABASE_URL" \
  -e UI_USERNAME="${LITELLM_UI_USERNAME:-admin}" \
  -e UI_PASSWORD="${LITELLM_UI_PASSWORD:-sk-nannos-local}" \
  -e AWS_BEDROCK_REGION="${AWS_BEDROCK_REGION:-eu-central-1}" \
  -e AWS_REGION="${AWS_BEDROCK_REGION:-eu-central-1}" \
  ${_GW_AWS_ENV[@]+"${_GW_AWS_ENV[@]}"} \
  -e AZURE_API_BASE="${AZURE_API_BASE:-}" \
  -e AZURE_OPENAI_API_KEY="${AZURE_OPENAI_API_KEY:-}" \
  -e AZURE_AI_API_BASE="${AZURE_AI_API_BASE:-}" \
  -e AZURE_AI_API_KEY="${AZURE_AI_API_KEY:-}" \
  -e GCP_PROJECT_ID="${GCP_PROJECT_ID:-}" \
  -e GCP_KEY="${GCP_KEY:-}" \
  ${_GW_GCP_ENV[@]+"${_GW_GCP_ENV[@]}"} \
  -e DEFAULT_VERTEXAI_LOCATION="${DEFAULT_VERTEXAI_LOCATION:-eu}" \
  -e CONSOLE_BACKEND_URL="http://host.docker.internal:${CONSOLE_BACKEND_PORT}" \
  -e GATEWAY_INGEST_TOKEN="$GATEWAY_INGEST_TOKEN" \
  "$_LITELLM_IMAGE" --config /etc/litellm/config.yaml --port 4000 >/dev/null

# Wait for the gateway to come up (its Python import alone can take ~50 s on a busy machine).
for _i in $(seq 1 90); do
  if curl -sf "http://localhost:${LLM_GATEWAY_PORT}/health/liveliness" >/dev/null 2>&1; then
    ok "Model Gateway ready at $LLM_GATEWAY_URL"
    ok "  LiteLLM admin UI: ${LLM_GATEWAY_URL}/ui (user: ${LITELLM_UI_USERNAME:-admin} / pass: ${LITELLM_UI_PASSWORD:-sk-nannos-local})"
    break
  fi
  if [[ "$_i" == "90" ]]; then
    err "Model Gateway did not become ready. Check: docker logs $_GW_CONTAINER"
  fi
  sleep 2
done

# ─── 8. Launch services via mprocs ─────────────────────────────────

cd "$ROOT_DIR"

if [[ -n "$_HEADLESS" ]]; then
  log "Starting all services in the background (slot $_SLOT)..."
else
  log "Starting all services with mprocs..."
fi
printf "\n"

# ── Resolve optional env vars ──
# OPENAI_COMPATIBLE_* are gateway-config inputs only (a `local` model_list alias, see
# above) — they're NOT exported to the services, which reach the local model through the
# gateway like any other provider. Kept as plain shell vars for the status line.
OPENAI_COMPATIBLE_BASE_URL="${OPENAI_COMPATIBLE_BASE_URL:-}"
OPENAI_COMPATIBLE_MODEL="${OPENAI_COMPATIBLE_MODEL:-}"
export MCP_GATEWAY_URL="${MCP_GATEWAY_URL:-}"
export MCP_GATEWAY_CLIENT_ID="${MCP_GATEWAY_CLIENT_ID:-gatana}"
export LANGSMITH_TRACING="${LANGSMITH_TRACING:-false}"
export LANGSMITH_API_KEY="${LANGSMITH_API_KEY:-}"
export LANGSMITH_PROJECT="${LANGSMITH_PROJECT:-}"
export LANGSMITH_ENDPOINT="${LANGSMITH_ENDPOINT:-}"
export LANGSMITH_ORGANIZATION_ID="${LANGSMITH_ORGANIZATION_ID:-}"
export LANGSMITH_PROJECT_ID="${LANGSMITH_PROJECT_ID:-}"
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

# A copy of slot 0's databases holds slot 0's catalogs, whose vectors and thumbnails live in the
# same S3 buckets under the same ids: a slot must not re-sync them on its own schedule. Nor may
# it act on slot 0's IdP groups and users.
_CATALOG_AUTO_SYNC=true
if [[ -f "$_DB_STATE.from-slot0" ]]; then
  _CATALOG_AUTO_SYNC=false
  # The copy's groups carry slot 0's IdP group ids, and its users slot 0's identities: with the
  # IdP admin client, editing a group or a phone number in the copy would change slot 0's.
  _OIDC_SECRET_ADMIN=""
  export OUTBOUND_SCIM_NIGHTLY_SYNC_ENABLED=false
  warn "Slot $_SLOT is a copy of slot 0: IdP group and user sync and outbound SCIM are off"
fi

# ── Generate mprocs config ──
if [[ "$_SLOT" != 0 ]]; then
  MPROCS_CFG="$_SLOT_DIR/procs.yaml"
else
  MPROCS_CFG=$(mktemp /tmp/nannos-mprocs-XXXXXX)
  mv "$MPROCS_CFG" "${MPROCS_CFG}.yaml"
  MPROCS_CFG="${MPROCS_CFG}.yaml"
fi

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
  [[ -n "$GCP_KEY" ]]              && _LLM_LINES="${_LLM_LINES}    ✓ GCP Vertex AI"$'\n'
fi
# Azure no longer depends on AWS: the key can come from .env alone.
[[ -n "$AZURE_AI_API_KEY" ]] && _LLM_LINES="${_LLM_LINES}    ✓ Azure (Nannos AI Foundry)"$'\n'

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
    backend .......... localhost:${_P_DBG_BACKEND}
    orchestrator ..... localhost:${_P_DBG_ORCHESTRATOR}
    runner ........... localhost:${_P_DBG_RUNNER}
    voice-agent ...... localhost:${_P_DBG_VOICE}
"
fi

# Prepare Slack (slot 0 only: the channel clients each bind one external app)
if [[ "$_SLOT" == 0 ]]; then
  pushd "$ROOT_DIR/packages/client-slack"
  just prepare-start
  popd
fi

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
    Console ........... ${_FRONTEND_URL}
    Backend API ....... http://localhost:${CONSOLE_BACKEND_PORT}
    Orchestrator ...... http://localhost:${_P_ORCHESTRATOR}
    Agent Runner ...... http://localhost:${_P_RUNNER}
    Voice Agent ....... http://localhost:${_P_VOICE}
    soffice-worker .... http://localhost:${_P_SOFFICE}
    Model Gateway ..... $LLM_GATEWAY_URL  (LiteLLM proxy — see the 'litellm' tab)
    Keycloak .......... $_KC_BASE_URL
    PostgreSQL (console)       localhost:5401
    PostgreSQL (docstore)      localhost:5402  (also holds checkpoints)

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
    1. Open ${_FRONTEND_URL} in your browser
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

  litellm:
    # The Model Gateway runs as a detached Docker container (started in §7b),
    # so it has no foreground process of its own. Stream its container logs into
    # a tab + logs/litellm.log so it's visible alongside the other services.
    shell: "docker logs -f $_GW_CONTAINER 2>&1 | tee $_LOG_DIR/litellm.log"
    stop: "SIGKILL"

  console-backend:
    cwd: "$ROOT_DIR/packages/console-backend"
    shell: "uv run python${_DEBUG_MODE:+ -m debugpy --listen 0.0.0.0:${_P_DBG_BACKEND}} -m uvicorn app:asgi_app --host 0.0.0.0 --port $CONSOLE_BACKEND_PORT --reload 2>&1 | tee $_LOG_DIR/console-backend.log"
    env:
      # Read back at runtime by the CORS allowlist and the loopback MCP client.
      CONSOLE_BACKEND_PORT: "$CONSOLE_BACKEND_PORT"
      FIRST_USER_IS_ADMIN: "true"
      OIDC_ISSUER: "$_OIDC_ISSUER"
      OIDC_CLIENT_ID: "agent-console"
      OIDC_CLIENT_SECRET: "$_OIDC_SECRET_BACKEND"
      OIDC_AUDIENCE: "agent-console"
      ORCHESTRATOR_CLIENT_ID: "orchestrator"
      BASE_DOMAIN: "localhost:${_P_FRONTEND}"
      FRONTEND_URL: "${_FRONTEND_URL}"
      ORCHESTRATOR_BASE_DOMAIN: "localhost:${_P_ORCHESTRATOR}"
      ORCHESTRATOR_ENVIRONMENT: "local"
      KEYCLOAK_ADMIN_CLIENT_ID: "nannos-admin"
      KEYCLOAK_ADMIN_CLIENT_SECRET: "$_OIDC_SECRET_ADMIN"
      KEYCLOAK_GROUP_NAME_PREFIX: "${_GROUP_PREFIX}"
      POSTGRES_HOST: "localhost"
      POSTGRES_PORT: "5401"
      POSTGRES_DB: "${_DB_CONSOLE}"
      POSTGRES_USER: "postgres"
      POSTGRES_PASSWORD: "password"
      POSTGRES_SCHEMA: "public"
      SCHEDULER_TICK_INTERVAL_SECONDS: "30"
      SCHEDULER_CLAIM_LIMIT: "10"
      AGENT_RUNNER_URL: "http://localhost:${_P_RUNNER}"
      LOG_LEVEL: "INFO"
      AZURE_OPENAI_API_KEY: "$AZURE_OPENAI_API_KEY"
      AZURE_API_BASE: "$AZURE_API_BASE"
      AWS_BEDROCK_REGION: "$AWS_BEDROCK_REGION"
      FILES_S3_BUCKET: "$FILES_S3_BUCKET"
      OBJECT_STORAGE_TYPE: "$OBJECT_STORAGE_TYPE"
      LOCAL_STORAGE_BASE_URL: "$LOCAL_STORAGE_BASE_URL"
      LOCAL_STORAGE_PATH: "$LOCAL_STORAGE_PATH"
      VOICE_AGENT_URL: "http://localhost:${_P_VOICE}"
      CATALOG_VECTOR_BUCKET_NAME: "$CATALOG_VECTOR_BUCKET_NAME"
      CATALOG_THUMBNAILS_S3_BUCKET: "$CATALOG_THUMBNAILS_S3_BUCKET"
      CATALOG_VECTOR_STORE_BACKEND: "s3_vectors"
      CATALOG_SUMMARIZATION_MODEL_ID: "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
      GOOGLE_OAUTH_CLIENT_ID: "$GOOGLE_OAUTH_CLIENT_ID"
      GOOGLE_OAUTH_CLIENT_SECRET: "$GOOGLE_OAUTH_CLIENT_SECRET"
      GOOGLE_OAUTH_REDIRECT_URI: "http://localhost:${CONSOLE_BACKEND_PORT}/api/v1/catalogs/connect/callback"
      TWILIO_ACCOUNT_SID: "$TWILIO_ACCOUNT_SID"
      TWILIO_VERIFY_SERVICE_SID: "$TWILIO_VERIFY_SERVICE_SID"
      TWILIO_VERIFY_API_KEY: "$TWILIO_VERIFY_API_KEY"
      TWILIO_VERIFY_API_SECRET: "$TWILIO_VERIFY_API_SECRET"
      AUTO_APPROVE_MAX_SYSTEM_PROMPT_LENGTH: "${AUTO_APPROVE_MAX_SYSTEM_PROMPT_LENGTH:-500}"
      AUTO_APPROVE_MAX_MCP_TOOLS_COUNT: "${AUTO_APPROVE_MAX_MCP_TOOLS_COUNT:-3}"
      LANGSMITH_ORGANIZATION_ID: "${LANGSMITH_ORGANIZATION_ID:-}"
      LANGSMITH_PROJECT_ID: "${LANGSMITH_PROJECT_ID:-}"
      DOCSTORE_HOST: "localhost"
      DOCSTORE_PORT: "5402"
      DOCSTORE_DB: "${_DB_DOCSTORE}"
      DOCSTORE_USER: "postgres"
      DOCSTORE_PASSWORD: "password"

  catalog-worker:
    cwd: "$ROOT_DIR/packages/console-backend"
    shell: "uv run python${_MEMPROFILE:+ -m memray run --native --follow-fork -o memray-worker.bin} catalog_worker.py 2>&1 | tee $_LOG_DIR/catalog-worker.log"
    env:
      POSTGRES_HOST: "localhost"
      POSTGRES_PORT: "5401"
      POSTGRES_DB: "${_DB_CONSOLE}"
      POSTGRES_USER: "postgres"
      POSTGRES_PASSWORD: "password"
      POSTGRES_SCHEMA: "public"
      CONSOLE_BACKEND_URL: "http://localhost:${CONSOLE_BACKEND_PORT}"
      SOFFICE_WORKER_URL: "http://localhost:${_P_SOFFICE}"
      CATALOG_VECTOR_BUCKET_NAME: "$CATALOG_VECTOR_BUCKET_NAME"
      CATALOG_THUMBNAILS_S3_BUCKET: "$CATALOG_THUMBNAILS_S3_BUCKET"
      CATALOG_VECTOR_STORE_BACKEND: "s3_vectors"
      CATALOG_SUMMARIZATION_MODEL_ID: "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
      CATALOG_AUTO_SYNC_ENABLED: "${_CATALOG_AUTO_SYNC}"
      CATALOG_SYNC_INTERVAL_SECONDS: "86400"
      CATALOG_SYNC_TICK_INTERVAL_SECONDS: "300"
      CATALOG_SYNC_MAX_CONCURRENT: "3"
      CATALOG_WORKER_POLL_INTERVAL: "5"
      GOOGLE_OAUTH_CLIENT_ID: "$GOOGLE_OAUTH_CLIENT_ID"
      GOOGLE_OAUTH_CLIENT_SECRET: "$GOOGLE_OAUTH_CLIENT_SECRET"
      AZURE_OPENAI_API_KEY: "$AZURE_OPENAI_API_KEY"
      AZURE_API_BASE: "$AZURE_API_BASE"
      AWS_BEDROCK_REGION: "$AWS_BEDROCK_REGION"
      LOG_LEVEL: "INFO"

  soffice-worker:
    cwd: "$ROOT_DIR/packages/soffice-worker"
    shell: "uv run uvicorn main:app --host 127.0.0.1 --port ${_P_SOFFICE} --reload 2>&1 | tee $_LOG_DIR/soffice-worker.log"
    env:
      PORT: "${_P_SOFFICE}"
      LOG_LEVEL: "INFO"

  orchestrator:
    cwd: "$ROOT_DIR/packages/orchestrator-agent"
    shell: "uv run python${_DEBUG_MODE:+ -m debugpy --listen 0.0.0.0:${_P_DBG_ORCHESTRATOR}} -m uvicorn main:app --host 0.0.0.0 --port ${_P_ORCHESTRATOR} --reload --reload-dir . --reload-dir ../agent-common/agent_common --log-config log_conf.yml --no-access-log 2>&1 | tee $_LOG_DIR/orchestrator.log"
    env:
      OIDC_ISSUER: "$_OIDC_ISSUER"
      OIDC_CLIENT_ID: "orchestrator"
      OIDC_CLIENT_SECRET: "$_OIDC_SECRET_ORCHESTRATOR"
      ORCHESTRATOR_CLIENT_ID: "orchestrator"
      AGENT_ID: "1"
      AGENT_BASE_URL: "http://localhost:${_P_ORCHESTRATOR}"
      CONSOLE_BACKEND_URL: "http://localhost:${CONSOLE_BACKEND_PORT}"
      CONSOLE_FRONTEND_URL: "${_FRONTEND_URL}"
      POSTGRES_HOST: "localhost"
      POSTGRES_PORT: "5402"
      POSTGRES_DB: "${_DB_DOCSTORE}"
      POSTGRES_USER: "postgres"
      POSTGRES_PASSWORD: "password"
      POSTGRES_SCHEMA: "public"
      MCP_GATEWAY_URL: "$MCP_GATEWAY_URL"
      MCP_GATEWAY_CLIENT_ID: "$MCP_GATEWAY_CLIENT_ID"
      LANGSMITH_TRACING: "$LANGSMITH_TRACING"
      LANGSMITH_API_KEY: "$LANGSMITH_API_KEY"
      LANGSMITH_PROJECT: "$LANGSMITH_PROJECT"
      LANGSMITH_ENDPOINT: "$LANGSMITH_ENDPOINT"
      LOG_LEVEL: "INFO"
      USE_SHORT_PROMPTS: "true"
      # Enable the wasm-sandboxed 'eval' REPL. When on, the orchestrator reaches
      # all its tools through 'eval' (opinionated single exposure model — see
      # graph_factory); when off, tools stay natively bound. Set in the repo-root .env.
      CODE_INTERPRETER_PTC: "${CODE_INTERPRETER_PTC:-0}"
      AZURE_OPENAI_API_KEY: "$AZURE_OPENAI_API_KEY"
      AZURE_API_BASE: "$AZURE_API_BASE"
      AWS_BEDROCK_REGION: "$AWS_BEDROCK_REGION"
      GCP_KEY: '$GCP_KEY'
      GCP_LOCATION: "$GCP_LOCATION"
      # Checkpointer reuses the POSTGRES_* connection above (docstore DB / public schema)
      CHECKPOINT_S3_BUCKET_NAME: "$CHECKPOINT_S3_BUCKET_NAME"
      DOCUMENT_STORE_S3_BUCKET: "$DOCUMENT_STORE_S3_BUCKET"
      OBJECT_STORAGE_TYPE: "$OBJECT_STORAGE_TYPE"
      LOCAL_STORAGE_BASE_URL: "$LOCAL_STORAGE_BASE_URL"
      LOCAL_STORAGE_PATH: "$LOCAL_STORAGE_PATH"
      CATALOG_VECTOR_BUCKET_NAME: "$CATALOG_VECTOR_BUCKET_NAME"
      CATALOG_THUMBNAILS_S3_BUCKET: "$CATALOG_THUMBNAILS_S3_BUCKET"
      SANDBOX_PROVIDER: "${SANDBOX_PROVIDER:-}"
      SANDBOX_POOL_CAPACITY: "${SANDBOX_POOL_CAPACITY:-}"
      SANDBOX_WARM_TTL: "${SANDBOX_WARM_TTL:-}"
      GATANA_ORG_ID: "${GATANA_ORG_ID:-}"
      GATANA_API_KEY: "${GATANA_API_KEY:-}"
      GATANA_ORG_CAPACITY: "${GATANA_ORG_CAPACITY:-}"

  runner:
    cwd: "$ROOT_DIR/packages/agent-runner"
    shell: "uv run python${_DEBUG_MODE:+ -m debugpy --listen 0.0.0.0:${_P_DBG_RUNNER}} -m uvicorn main:app --host 0.0.0.0 --port ${_P_RUNNER} --reload --reload-dir . --reload-dir ../agent-common/agent_common --log-config log_conf.yml --no-access-log 2>&1 | tee $_LOG_DIR/runner.log"
    env:
      OIDC_ISSUER: "$_OIDC_ISSUER"
      OIDC_CLIENT_ID: "agent-runner"
      OIDC_CLIENT_SECRET: "$_OIDC_SECRET_AGENT_RUNNER"
      AGENT_BASE_URL: "http://localhost:${_P_RUNNER}"
      CONSOLE_BACKEND_URL: "http://localhost:${CONSOLE_BACKEND_PORT}"
      POSTGRES_HOST: "localhost"
      POSTGRES_PORT: "5402"
      POSTGRES_DB: "${_DB_DOCSTORE}"
      POSTGRES_USER: "postgres"
      POSTGRES_PASSWORD: "password"
      POSTGRES_SCHEMA: "public"
      MCP_GATEWAY_URL: "$MCP_GATEWAY_URL"
      MCP_GATEWAY_CLIENT_ID: "$MCP_GATEWAY_CLIENT_ID"
      LANGSMITH_TRACING: "$LANGSMITH_TRACING"
      LANGSMITH_API_KEY: "$LANGSMITH_API_KEY"
      LANGSMITH_PROJECT: "$LANGSMITH_PROJECT"
      LANGSMITH_ENDPOINT: "$LANGSMITH_ENDPOINT"
      LOG_LEVEL: "INFO"
      AZURE_OPENAI_API_KEY: "$AZURE_OPENAI_API_KEY"
      AZURE_API_BASE: "$AZURE_API_BASE"
      AWS_BEDROCK_REGION: "$AWS_BEDROCK_REGION"
      GCP_KEY: '$GCP_KEY'
      GCP_LOCATION: "$GCP_LOCATION"
      # Checkpointer reuses the POSTGRES_* connection above (docstore DB / public schema)
      CHECKPOINT_S3_BUCKET_NAME: "$CHECKPOINT_S3_BUCKET_NAME"
      DOCUMENT_STORE_S3_BUCKET: "$DOCUMENT_STORE_S3_BUCKET"
      SANDBOX_PROVIDER: "${SANDBOX_PROVIDER:-}"
      SANDBOX_POOL_CAPACITY: "${SANDBOX_POOL_CAPACITY:-}"
      SANDBOX_WARM_TTL: "${SANDBOX_WARM_TTL:-}"
      GATANA_ORG_ID: "${GATANA_ORG_ID:-}"
      GATANA_API_KEY: "${GATANA_API_KEY:-}"
      GATANA_ORG_CAPACITY: "${GATANA_ORG_CAPACITY:-}"

  voice-agent:
    cwd: "$ROOT_DIR/packages/voice-agent"
    shell: "uv run python${_DEBUG_MODE:+ -m debugpy --listen 0.0.0.0:${_P_DBG_VOICE}} main.py  --reload 2>&1 | tee $_LOG_DIR/voice-agent.log"
    env:
      HOST: "localhost"
      PORT: "${_P_VOICE}"
      OIDC_ISSUER: "$_OIDC_ISSUER"
      OIDC_CLIENT_ID: "voice-agent"
      VOICE_AGENT_BASE_URL: "http://localhost:${_P_VOICE}"
      CONSOLE_FRONTEND_URL: "${_FRONTEND_URL}"
      CONSOLE_BACKEND_URL: "http://localhost:${CONSOLE_BACKEND_PORT}"
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
      TWILIO_VERIFY_API_KEY: "$TWILIO_VERIFY_API_KEY"
      TWILIO_VERIFY_API_SECRET: "$TWILIO_VERIFY_API_SECRET"
      LANGSMITH_TRACING: "$LANGSMITH_TRACING"
      LANGSMITH_API_KEY: "$LANGSMITH_API_KEY"
      LANGSMITH_PROJECT: "$LANGSMITH_PROJECT"
      LANGSMITH_ENDPOINT: "$LANGSMITH_ENDPOINT"
      LOG_LEVEL: "INFO"

  # 'dev:link', NOT 'build:watch'. The console dev server compiles the SDK's
  # source directly, so a 'vite build --watch' here would rebuild a dist nobody
  # reads — while rewriting it non-atomically with renumbered rollup chunks,
  # which is what used to wedge the dev server mid-rebuild. dev:link keeps the
  # .d.ts (editor/tsc) and dist/styles.css (shadow-DOM embed mounts) in sync and
  # leaves dist/*.js alone. Consequence: dist JS goes stale during a session —
  # re-run 'npm run build' in packages/embed-sdk before a console prod build.
  embed-sdk:
    cwd: "$ROOT_DIR/packages/embed-sdk"
    shell: "npm run dev:link 2>&1 | tee $_LOG_DIR/embed-sdk.log"
    stop: "SIGKILL"

  frontend:
    cwd: "$ROOT_DIR/packages/console-frontend"
    shell: "npx vite --host 0.0.0.0 --port ${_P_FRONTEND}${_HEADLESS:+ --strictPort} 2>&1 | tee $_LOG_DIR/frontend.log"
YAML

# The shared infrastructure's logs and the channel clients belong to slot 0: the clients each bind
# one external app (a Slack app, a Google Chat bot), which a second stack cannot share.
if [[ "$_SLOT" == 0 ]]; then
cat >> "$MPROCS_CFG" <<YAML

  infra-logs:
    cwd: "$LOCAL_DEV_DIR"
    shell: "docker compose logs -f 2>&1 | tee $_LOG_DIR/infra.log"
    stop: "SIGKILL"
  
  slack:
    cwd: "$ROOT_DIR/packages/client-slack"
    shell: "just start 2>&1 | tee $_LOG_DIR/slack.log"
    stop: "SIGKILL"

  google-chat:
    cwd: "$ROOT_DIR/packages/client-google-chat"
    shell: "npm run dev"
    stop: "SIGKILL"
YAML
fi

if [[ -z "$_HEADLESS" ]]; then
  exec mprocs --config "$MPROCS_CFG"
fi

# ─── 9. Headless: start in the background, wait until healthy ──────
# The procs file is the single definition of what runs; slot_procs.py starts each entry as its
# own process group (stop = kill the group) and records the PIDs beside the claim.
slot_procs start "$MPROCS_CFG" "$_SLOT_DIR" --skip info

# Any HTTP answer counts as up (the orchestrator answers 401 without a token); 000 is no answer.
_SLOT_CHECKS="console-backend=http://localhost:${CONSOLE_BACKEND_PORT}/api/v1/health
frontend=${_FRONTEND_URL}/
orchestrator=http://localhost:${_P_ORCHESTRATOR}/
runner=http://localhost:${_P_RUNNER}/
soffice-worker=http://localhost:${_P_SOFFICE}/health
voice-agent=http://localhost:${_P_VOICE}/"
_DEADLINE=$((SECONDS + ${NANNOS_SLOT_HEALTH_TIMEOUT:-420}))
while :; do
  _DOWN=""
  while IFS='=' read -r _name _url; do
    _code=$(curl -s -o /dev/null --max-time 3 -w '%{http_code}' "$_url" || true)
    [[ "$_code" != "000" ]] || _DOWN="$_DOWN $_name"
  done <<< "$_SLOT_CHECKS"
  [[ -n "$_DOWN" ]] || break
  # A service that exited will not come up by waiting: fail now, not at the deadline.
  if ! _EXITED="$(slot_procs status "$_SLOT_DIR" --all 2>/dev/null)"; then
    err "Slot $_SLOT: a service exited during startup ($(echo "$_EXITED" | grep ': down' | cut -d: -f1 | xargs)). Logs: $_LOG_DIR"
  fi
  if [[ $SECONDS -ge $_DEADLINE ]]; then
    err "Slot $_SLOT not healthy:${_DOWN}. Logs: $_LOG_DIR"
  fi
  sleep 3
done
ok "Slot $_SLOT is up"

python3 - "$_SLOT_DIR/slot.json" <<PYJSON
import json, sys
summary = {
    "slot": $_SLOT,
    "worktree": "$ROOT_DIR",
    "console": "$_FRONTEND_URL",
    "backend": "http://localhost:$CONSOLE_BACKEND_PORT",
    "orchestrator": "http://localhost:$_P_ORCHESTRATOR",
    "runner": "http://localhost:$_P_RUNNER",
    "gateway": "http://localhost:$LLM_GATEWAY_PORT",
    "databases": {"console": "$_DB_CONSOLE", "docstore": "$_DB_DOCSTORE"},
    "postgres": {"console": "localhost:5401", "docstore": "localhost:5402"},
    "idp": "$_OIDC_ISSUER",
    "logs": "$_LOG_DIR",
}
if "$_DEBUG_MODE":
    summary["debugpy"] = {"backend": $_P_DBG_BACKEND, "orchestrator": $_P_DBG_ORCHESTRATOR, "runner": $_P_DBG_RUNNER, "voice-agent": $_P_DBG_VOICE}
with open(sys.argv[1], "w") as f:
    json.dump(summary, f, indent=2)
PYJSON

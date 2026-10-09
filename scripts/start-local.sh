#!/usr/bin/env bash
set -euo pipefail

# ─── Nannos Local Development Startup ──────────────────────────────
#
# Starts all services from a clean clone, as one process-compose stack
# (scripts/local-dev/process-compose.yaml): this script assembles its configuration and
# secrets, process-compose runs the setup steps and the services.
# Prerequisites: docker, uv, node/npm, process-compose
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
#   --headless   Start the stack detached instead of in the process-compose TUI, wait until it
#                is ready and exit (slots only).
#   --local-idp  Use the local Keycloak even when .env names a remote OIDC_ISSUER.
#   --yes        Start without asking to confirm the plan (`just start-local` passes it).
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
#   Any other LiteLLM provider - put its env vars (e.g. DEEPSEEK_API_KEY) in .env.gateway, which
#                              only the gateway container reads
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
_ASSUME_YES=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --debug) _DEBUG_MODE=1; shift ;;
    --slot) _SLOT="${2:-}"; shift 2 ;;
    --headless) _HEADLESS=1; shift ;;
    --local-idp) _FORCE_LOCAL_IDP=1; shift ;;
    -y|--yes) _ASSUME_YES=1; shift ;;
    *) echo "Unknown flag: $1"; exit 1 ;;
  esac
done
if [[ ! "$_SLOT" =~ ^[0-8]$ ]]; then
  echo "--slot takes 1-8 (slot 0 is the default stack)"; exit 1
fi
if [[ -n "$_HEADLESS" && "$_SLOT" == 0 ]]; then
  echo "--headless runs a slot (1-8); use 'just up'"; exit 1
fi
# A slot runs headless only: its claim and its stack are driven by `just up` / `just down`.
if [[ "$_SLOT" != 0 && -z "$_HEADLESS" ]]; then
  echo "A slot runs headless; start it with 'just up'"; exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOCAL_DEV_DIR="$SCRIPT_DIR/local-dev"
# shellcheck source=local-dev/slot-common.sh
source "$LOCAL_DEV_DIR/slot-common.sh"

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
# Slot 0's backend and gateway ports can be set in the environment or .env (a slot's cannot).
_ENV_BACKEND_PORT="${CONSOLE_BACKEND_PORT:-}"
_ENV_GATEWAY_PORT="${LLM_GATEWAY_PORT:-}"

# ─── 0b. Stack slot (ADR-0016) ─────────────────────────────────────
# Slot 0 is the stack this script has always started. Slots 1-8 run beside it and each other:
# slot N owns the port block 4N000-4N999, its own databases on the shared Postgres servers, its
# own Model Gateway container, its own cookie names and IdP group prefix.
_STACK_DIR="$(slot_dir "$_SLOT")"
_SOCK="$(slot_sock "$_SLOT")"
export CONSOLE_BACKEND_PORT="$(slot_port "$_SLOT" backend)"
LLM_GATEWAY_PORT="$(slot_port "$_SLOT" gateway)"
_P_FRONTEND="$(slot_port "$_SLOT" frontend)"; _P_ORCHESTRATOR="$(slot_port "$_SLOT" orchestrator)"
_P_RUNNER="$(slot_port "$_SLOT" runner)"; _P_VOICE="$(slot_port "$_SLOT" voice)"
_P_SOFFICE="$(slot_port "$_SLOT" soffice)"
_P_DBG_BACKEND="$(slot_port "$_SLOT" dbg_backend)"; _P_DBG_ORCHESTRATOR="$(slot_port "$_SLOT" dbg_orchestrator)"
_P_DBG_RUNNER="$(slot_port "$_SLOT" dbg_runner)"; _P_DBG_VOICE="$(slot_port "$_SLOT" dbg_voice)"
if [[ "$_SLOT" == 0 ]]; then
  # Overridable for when something else already holds the default:
  #   CONSOLE_BACKEND_PORT=5002 ./scripts/start-local.sh
  # Exported so the frontend's vite proxy picks the same value up.
  export CONSOLE_BACKEND_PORT="${_ENV_BACKEND_PORT:-$CONSOLE_BACKEND_PORT}"
  LLM_GATEWAY_PORT="${_ENV_GATEWAY_PORT:-$LLM_GATEWAY_PORT}"
  _GW_CONTAINER="nannos-litellm-proxy-local"
  _GROUP_PREFIX="local-"
  _LOG_DIR="$ROOT_DIR/logs"
  if slot_pc_running 0; then
    _RUNNING_IN="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["worktree"])' "$_STACK_DIR/stack.json" 2>/dev/null || echo "another checkout")"
    echo "Slot 0 already runs (from $_RUNNING_IN). Stop it first: quit its TUI, or 'just stop-local'."; exit 1
  fi
  # Another stack on slot 0's ports — one started before process-compose (mprocs), or anything
  # else — would make every service fail on "address in use", and the gateway step would remove
  # its gateway container (same name). Refuse before touching anything. A gateway container of
  # ours left alone on its port is a leftover the gateway step replaces.
  _HELD=""
  for _name in backend frontend orchestrator runner voice soffice gateway; do
    _port="$(slot_port 0 "$_name")"
    [[ "$_name" == backend ]] && _port="$CONSOLE_BACKEND_PORT"
    [[ "$_name" == gateway ]] && _port="$LLM_GATEWAY_PORT"
    _pid="$(lsof -nP -iTCP:"$_port" -sTCP:LISTEN -t 2>/dev/null | head -1 || true)"
    [[ -n "$_pid" ]] || continue
    _container="$(docker ps --filter "publish=$_port" --format '{{.Names}}' 2>/dev/null | head -1 || true)"
    if [[ -n "$_container" ]]; then
      [[ "$_name" == gateway && "$_container" == "$_GW_CONTAINER" ]] && continue
      _HELD="$_HELD
  :$_port ($_name) — Docker container $_container"
    else
      _cwd="$(lsof -a -p "$_pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' || true)"
      _where="$(git -C "${_cwd:-/}" rev-parse --show-toplevel 2>/dev/null || echo "${_cwd:-unknown directory}")"
      _HELD="$_HELD
  :$_port ($_name) — $(ps -o comm= -p "$_pid" 2>/dev/null | xargs basename 2>/dev/null) (pid $_pid) in $_where"
    fi
  done
  if [[ -n "$_HELD" ]]; then
    echo "Slot 0's ports are in use — another stack (an mprocs-era start-local?) or another process:$_HELD"
    echo "Stop it first (quit its mprocs/TUI, or 'just stop-local')."; exit 1
  fi
else
  [[ -f "$_STACK_DIR/claim.json" ]] || { echo "Slot $_SLOT is not claimed. Start a slot with 'just up'."; exit 1; }
  _GW_CONTAINER="nannos-gw-s${_SLOT}"
  _GROUP_PREFIX="local-s${_SLOT}-"
  _LOG_DIR="$_STACK_DIR/logs"
  # Browsers scope cookies by host, not port: without their own names, slots on localhost would
  # sign each other out. The browser's Origin is the slot's frontend, which Socket.IO checks.
  export SESSION_COOKIE_NAME="a2a-chatui-s${_SLOT}"
  export OAUTH_STATE_COOKIE_NAME="session-s${_SLOT}"
  export CORS_ALLOWED_CHAT_ORIGINS="${CORS_ALLOWED_CHAT_ORIGINS:+$CORS_ALLOWED_CHAT_ORIGINS,}http://localhost:${_P_FRONTEND},http://127.0.0.1:${_P_FRONTEND}"
  # Uploads live and die with the slot's databases — whatever the environment or .env says, which
  # would otherwise share one directory between slots.
  LOCAL_STORAGE_PATH="$_STACK_DIR/uploads"
fi
_DB_CONSOLE="$(slot_db_name "$_SLOT" console)"; _DB_DOCSTORE="$(slot_db_name "$_SLOT" docstore)"
_FRONTEND_URL="http://localhost:${_P_FRONTEND}"
mkdir -p "$_STACK_DIR" "$_LOG_DIR"

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
command -v process-compose >/dev/null 2>&1 || missing+=(process-compose)

if [[ ${#missing[@]} -gt 0 ]]; then
  err "Missing required tools: ${missing[*]}
  Install them:
    brew install docker uv node f1bonacc1/tap/process-compose"
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
printf "${CYAN}│${RESET}    ${GREEN}✓${RESET} Model Gateway   ${DIM}(LiteLLM proxy, Docker, localhost:${LLM_GATEWAY_PORT}; process 'gateway')${RESET}\n"
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
if [[ -n "$_HEADLESS" || -n "$_ASSUME_YES" ]]; then
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

# ─── 4. Model Gateway configuration ──────────────────────────────
# The gateway itself is a process of the stack (scripts/local-dev/gateway.sh); its config and
# credentials are assembled here, where the secrets are.
export LLM_GATEWAY_PORT
export LLM_GATEWAY_URL="http://localhost:${LLM_GATEWAY_PORT}"
export LLM_GATEWAY_API_KEY="sk-nannos-local"
# Master key for the proxy management API (/model/*). Locally it equals the app key; in real envs
# they differ (master key only on proxy + console-backend), which console-backend reads.
export LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY:-sk-nannos-local}"
# Shared secret for proxy → console-backend cost ingestion (/api/v1/usage/gateway-batch-log).
export GATEWAY_INGEST_TOKEN="${GATEWAY_INGEST_TOKEN:-sk-nannos-local-ingest}"
# Pinned to match the prod base image (packages/litellm-proxy/Dockerfile) so local reproduces
# prod's Vertex region-resolution behavior. Override with LITELLM_IMAGE.
export NANNOS_LITELLM_IMAGE="${LITELLM_IMAGE:-ghcr.io/berriai/litellm:v1.103.0@sha256:bd089afdcd35b894b14a93f9743cdc8b591f82da1a38dd43a010a7b0c9de5fd7}"
# Local gateway DB: the stack's console database, `litellm` schema (created by migrate-console;
# mirrors the prod shared-RDS pattern). store_model_in_db lets the console register models at
# runtime — without it /model/new returns "No DB Connected". The container reaches the host
# Postgres (published on :5401) via host.docker.internal.
export LITELLM_DATABASE_URL="${LITELLM_DATABASE_URL:-postgresql://postgres:password@host.docker.internal:5401/${_DB_CONSOLE}?schema=litellm}"
# A slot's gateway keeps its registrations in its own database, whatever the environment says: a
# shared one would share model registrations and tier chains between stacks (ADR-0016).
[[ "$_SLOT" == 0 ]] || LITELLM_DATABASE_URL="postgresql://postgres:password@host.docker.internal:5401/${_DB_CONSOLE}?schema=litellm"

# Resolve a path to its physical location (symlinks expanded).
#
# On macOS /tmp is a symlink to /private/tmp. Docker Desktop shares the physical path, so
# bind-mounting a /tmp/... path can silently create an empty DIRECTORY inside the VM instead of
# mounting the file. The proxy then dies with "IsADirectoryError: '/etc/litellm/config.yaml'", and
# the GCP service-account mount fails *silently* — GOOGLE_APPLICATION_CREDENTIALS ends up pointing
# at a directory, so Vertex auth just doesn't work with nothing in the logs. The files live in the
# stack directory now, but $HOME (or NANNOS_SLOTS_DIR) can be a symlink too.
#
# `pwd -P` is POSIX and a no-op on Linux, where /tmp is a real directory.
_physical_path() { cd "$(dirname "$1")" && printf '%s/%s\n' "$(pwd -P)" "$(basename "$1")"; }

mkdir -p "$_STACK_DIR/gateway"
# The config holds a local LLM's API key and the SA file a GCP key: this user's eyes only.
chmod 700 "$_STACK_DIR/gateway"

# The gateway config is assembled for this start (never committed — it is deployment-specific).
# Three sources, each owning what only it can own:
#   settings     -> packages/litellm-proxy/litellm-settings.yaml (shared with deployments)
#   general_*    -> inline below (names env vars that differ per environment)
#   model_list   -> $LITELLM_LOCAL_MODELS_FILE when set, else the committed example.
_GW_CONFIG="$(_physical_path "$_STACK_DIR/gateway/config.yaml")"

# Settings come from the committed, SHARED settings file — the same blocks a deployment's own
# config.yaml is expected to carry. Do not inline them here: they used to live in this script AND
# in each deployment's mounted config, kept in step only by comments claiming they matched.
# Nothing verifies such a claim, and a deployment left on num_retries: 0 has no retries and no
# failover at all (nannos#204).
_GW_SETTINGS="$ROOT_DIR/packages/litellm-proxy/litellm-settings.yaml"
[[ -f "$_GW_SETTINGS" ]] || err "Missing gateway settings file: $_GW_SETTINGS"
cat "$_GW_SETTINGS" > "$_GW_CONFIG"

# general_settings stays here: it names env vars that legitimately differ per environment
# (locally LITELLM_DATABASE_URL, in k8s DATABASE_URL), so it cannot be shared verbatim.
cat >> "$_GW_CONFIG" <<'EOF'

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  store_model_in_db: true
  database_url: os.environ/LITELLM_DATABASE_URL
EOF

# model_list: deployment-specific (model ids, regions, provider deployments), so it lives in a
# gitignored YAML file rather than this committed script. Resolution order:
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
export NANNOS_GW_CONFIG="$_GW_CONFIG"

# The profile's (temporary) credentials, for the gateway container to reach Bedrock: the SDK's
# profile/SSO chain isn't visible inside it. Under their own names, so only the gateway gets them;
# the services keep resolving the profile, which refreshes.
unset NANNOS_GW_AWS_ACCESS_KEY_ID NANNOS_GW_AWS_SECRET_ACCESS_KEY NANNOS_GW_AWS_SESSION_TOKEN
if [[ "$_HAS_AWS" == true ]]; then
  if _CREDS=$(aws configure export-credentials --profile "$AWS_PROFILE" --format process 2>/dev/null); then
    eval "$(printf '%s' "$_CREDS" | python3 -c '
import json, shlex, sys
c = json.load(sys.stdin)
for var, key in (("ACCESS_KEY_ID", "AccessKeyId"), ("SECRET_ACCESS_KEY", "SecretAccessKey"), ("SESSION_TOKEN", "SessionToken")):
    if c.get(key):
        print(f"export NANNOS_GW_AWS_{var}={shlex.quote(c[key])}")
')"
  else
    warn "Could not export AWS credentials for the gateway — Bedrock models may not work locally"
  fi
fi

# Pod-level Vertex auth via ADC, mirroring deployment: see scripts/local-dev/gateway.sh.
unset NANNOS_GW_GCP_SA
rm -f "$_STACK_DIR/gateway/gcp-sa.json"
if [[ -n "${GCP_KEY:-}" ]]; then
  (umask 077 && printf '%s' "$GCP_KEY" > "$_STACK_DIR/gateway/gcp-sa.json")
  export NANNOS_GW_GCP_SA="$(_physical_path "$_STACK_DIR/gateway/gcp-sa.json")"
fi

# ─── 5. Stack environment ─────────────────────────────────────────
# What scripts/local-dev/process-compose.yaml reads (see its header), and what every process of
# the stack inherits. Values that differ between services are set per process there.

# OPENAI_COMPATIBLE_* are gateway-config inputs only (the `local` model above) — the services
# reach the local model through the gateway like any other provider.
unset OPENAI_COMPATIBLE_BASE_URL OPENAI_COMPATIBLE_MODEL OPENAI_COMPATIBLE_API_KEY
export MCP_GATEWAY_URL="${MCP_GATEWAY_URL:-}"
export MCP_GATEWAY_CLIENT_ID="${MCP_GATEWAY_CLIENT_ID:-gatana}"
export LANGSMITH_TRACING="${LANGSMITH_TRACING:-false}"
export LANGSMITH_API_KEY="${LANGSMITH_API_KEY:-}" LANGSMITH_PROJECT="${LANGSMITH_PROJECT:-}"
export LANGSMITH_ENDPOINT="${LANGSMITH_ENDPOINT:-}" LANGSMITH_ORGANIZATION_ID="${LANGSMITH_ORGANIZATION_ID:-}"
export LANGSMITH_PROJECT_ID="${LANGSMITH_PROJECT_ID:-}"
export AZURE_OPENAI_API_KEY AZURE_API_BASE AZURE_AI_API_KEY AZURE_AI_API_BASE AWS_BEDROCK_REGION
export GCP_KEY GCP_PROJECT_ID GCP_LOCATION
export CHECKPOINT_S3_BUCKET_NAME DOCUMENT_STORE_S3_BUCKET FILES_S3_BUCKET
export OBJECT_STORAGE_TYPE LOCAL_STORAGE_BASE_URL LOCAL_STORAGE_PATH
export CATALOG_VECTOR_BUCKET_NAME CATALOG_THUMBNAILS_S3_BUCKET
export GOOGLE_OAUTH_CLIENT_ID GOOGLE_OAUTH_CLIENT_SECRET
export TWILIO_ACCOUNT_SID TWILIO_API_KEY TWILIO_API_SECRET
export TWILIO_VERIFY_SERVICE_SID TWILIO_VERIFY_API_KEY TWILIO_VERIFY_API_SECRET
export AUTO_APPROVE_MAX_SYSTEM_PROMPT_LENGTH="${AUTO_APPROVE_MAX_SYSTEM_PROMPT_LENGTH:-500}"
export AUTO_APPROVE_MAX_MCP_TOOLS_COUNT="${AUTO_APPROVE_MAX_MCP_TOOLS_COUNT:-3}"
# Enable the wasm-sandboxed 'eval' REPL. When on, the orchestrator reaches all its tools through
# 'eval' (opinionated single exposure model — see graph_factory); when off, tools stay natively
# bound. Set in the repo-root .env.
export CODE_INTERPRETER_PTC="${CODE_INTERPRETER_PTC:-0}"
export PUBLIC_URL="${PUBLIC_URL:-}"
# Optional settings reach the services only when set: an empty one is not "unset" to them
# (agent-runner's sandbox pool fails on int("") of an empty SANDBOX_POOL_CAPACITY).
for _var in SANDBOX_PROVIDER SANDBOX_POOL_CAPACITY SANDBOX_WARM_TTL GATANA_ORG_ID GATANA_API_KEY GATANA_ORG_CAPACITY; do
  if [[ -n "${!_var:-}" ]]; then export "${_var?}"; else unset "$_var"; fi
done

export NANNOS_ROOT="$ROOT_DIR" NANNOS_SLOT="$_SLOT" NANNOS_STACK_DIR="$_STACK_DIR" NANNOS_LOG_DIR="$_LOG_DIR"
export NANNOS_FRONTEND_PORT="$_P_FRONTEND" NANNOS_ORCHESTRATOR_PORT="$_P_ORCHESTRATOR"
export NANNOS_RUNNER_PORT="$_P_RUNNER" NANNOS_VOICE_PORT="$_P_VOICE" NANNOS_SOFFICE_PORT="$_P_SOFFICE"
export NANNOS_FRONTEND_URL="$_FRONTEND_URL"
export NANNOS_DB_CONSOLE="$_DB_CONSOLE" NANNOS_DB_DOCSTORE="$_DB_DOCSTORE"
export NANNOS_GW_CONTAINER="$_GW_CONTAINER" NANNOS_GROUP_PREFIX="$_GROUP_PREFIX"
export NANNOS_OIDC_MODE="$_OIDC_MODE" NANNOS_OIDC_ISSUER="$_OIDC_ISSUER"
export NANNOS_OIDC_SECRET_BACKEND="$_OIDC_SECRET_BACKEND" NANNOS_OIDC_SECRET_ORCHESTRATOR="$_OIDC_SECRET_ORCHESTRATOR"
export NANNOS_OIDC_SECRET_ADMIN="$_OIDC_SECRET_ADMIN" NANNOS_OIDC_SECRET_RUNNER="$_OIDC_SECRET_AGENT_RUNNER"
export NANNOS_CATALOG_AUTO_SYNC=true
export NANNOS_MEMPROFILE_ARGS="${_MEMPROFILE:+-m memray run --native --follow-fork -o memray-worker.bin}"
for _svc in BACKEND ORCHESTRATOR RUNNER VOICE; do
  _var="_P_DBG_${_svc}"
  if [[ -n "$_DEBUG_MODE" ]]; then
    export "NANNOS_DEBUGPY_${_svc}=-m debugpy --listen 0.0.0.0:${!_var}"
  else
    export "NANNOS_DEBUGPY_${_svc}="
  fi
done
if [[ "$_SLOT" == 0 ]]; then
  export NANNOS_CHANNEL_CLIENTS_OFF=false
else
  export NANNOS_CHANNEL_CLIENTS_OFF=true
fi

# The stack's summary: what `just up` prints, `just slots` reads, and the `info` process shows.
python3 - "$_STACK_DIR/stack.json" <<PYJSON
import json, sys
summary = {
    "slot": $_SLOT,
    "worktree": "$ROOT_DIR",
    "console": "$_FRONTEND_URL",
    "backend": "http://localhost:$CONSOLE_BACKEND_PORT",
    "orchestrator": "http://localhost:$_P_ORCHESTRATOR",
    "runner": "http://localhost:$_P_RUNNER",
    "voice-agent": "http://localhost:$_P_VOICE",
    "gateway": "http://localhost:$LLM_GATEWAY_PORT",
    "databases": {"console": "$_DB_CONSOLE", "docstore": "$_DB_DOCSTORE"},
    "postgres": {"console": "localhost:5401", "docstore": "localhost:5402"},
    "idp": "$_OIDC_ISSUER",
    "logs": "$_LOG_DIR",
    "control": "process-compose attach -u $_SOCK",
}
if "$_DEBUG_MODE":
    summary["debugpy"] = {"backend": $_P_DBG_BACKEND, "orchestrator": $_P_DBG_ORCHESTRATOR, "runner": $_P_DBG_RUNNER, "voice-agent": $_P_DBG_VOICE}
with open(sys.argv[1], "w") as f:
    json.dump(summary, f, indent=2)
PYJSON

# Build scenario label for the info process
if [[ "$_OIDC_MODE" == "remote-ssm" ]]; then
  _SCENARIO="Local + AWS + Remote OIDC"
elif [[ "$_OIDC_MODE" == "remote-manual" ]]; then
  _SCENARIO="Local + Remote OIDC"
elif [[ "$_HAS_AWS" == true ]]; then
  _SCENARIO="Local + AWS"
else
  _SCENARIO="Full Local"
fi
if [[ "$_OIDC_MODE" == "local" ]]; then
  _AUTH_LINE="Local Keycloak (localhost:8180) — test@local.dev / password"
else
  _AUTH_LINE="Remote OIDC: $_OIDC_ISSUER"
fi
{
  printf '\n  Nannos local stack — slot %s (%s)\n\n' "$_SLOT" "$_SCENARIO"
  printf '  Console ........... %s\n' "$_FRONTEND_URL"
  printf '  Backend API ....... http://localhost:%s\n' "$CONSOLE_BACKEND_PORT"
  printf '  Orchestrator ...... http://localhost:%s\n' "$_P_ORCHESTRATOR"
  printf '  Agent Runner ...... http://localhost:%s\n' "$_P_RUNNER"
  printf '  Voice Agent ....... http://localhost:%s\n' "$_P_VOICE"
  printf '  soffice-worker .... http://localhost:%s\n' "$_P_SOFFICE"
  printf '  Model Gateway ..... %s  (admin UI /ui: %s / %s)\n' "$LLM_GATEWAY_URL" "${LITELLM_UI_USERNAME:-admin}" "${LITELLM_UI_PASSWORD:-sk-nannos-local}"
  printf '  Keycloak .......... %s\n' "$_KC_BASE_URL"
  printf '  PostgreSQL ........ localhost:5401/%s, localhost:5402/%s (also holds checkpoints)\n' "$_DB_CONSOLE" "$_DB_DOCSTORE"
  printf '\n  Authentication: %s\n' "$_AUTH_LINE"
  if [[ -n "$_DEBUG_MODE" ]]; then
    printf '  debugpy: backend=%s orchestrator=%s runner=%s voice-agent=%s\n' \
      "$_P_DBG_BACKEND" "$_P_DBG_ORCHESTRATOR" "$_P_DBG_RUNNER" "$_P_DBG_VOICE"
  fi
  printf '\n  Logs: %s/<service>.log\n' "$_LOG_DIR"
  printf '  Services reload when you edit code. Setup steps (infra, migrate-*, deps-*, ...) run once.\n'
  if [[ -z "$_HEADLESS" ]]; then
    printf '  Quit the TUI (F10, or Ctrl+C) to stop the stack.\n'
  fi
  printf '  From another terminal: process-compose attach -u %s\n\n' "$_SOCK"
} > "$_STACK_DIR/info.txt"

# ─── 6. Launch the stack (process-compose) ───────────────────────
cd "$ROOT_DIR"
# Each start begins its logs afresh (process-compose appends): a log shows this run only, not the
# errors of the last one.
rm -rf "$_STACK_DIR/ready"
for _log in $(sed -n 's|^ *log_location: ${NANNOS_LOG_DIR}/||p' "$LOCAL_DEV_DIR/process-compose.yaml"); do
  : > "$_LOG_DIR/$_log"
done
# The stack runs from a copy of its definition in the stack's directory, which is also what the
# slot's verdict reads: a branch switch in the checkout must not change either under a running stack.
cp "$LOCAL_DEV_DIR/process-compose.yaml" "$_STACK_DIR/process-compose.yaml"
_PC=(process-compose -f "$_STACK_DIR/process-compose.yaml" --disable-dotenv
     -L "$_STACK_DIR/process-compose.log" -u "$_SOCK")
rm -f "$_SOCK"

# Whatever is left of an earlier run of this stack (a process that outlived its shutdown) would
# hold a port or a database connection: stop it first.
slot_kill_leftovers "$_SLOT"

if [[ -z "$_HEADLESS" ]]; then
  log "Starting the stack with process-compose (slot $_SLOT)..."
  # Not exec: once the TUI is quit, stop anything of the stack that outlived the shutdown.
  _PC_EXIT=0
  "${_PC[@]}" up --hide-disabled || _PC_EXIT=$?
  slot_kill_leftovers "$_SLOT"
  rm -f "$_SOCK"
  exit "$_PC_EXIT"
fi

# Headless: start detached and wait until the stack is ready — or has failed, which does not get
# better by waiting.
log "Starting slot $_SLOT in the background..."
"${_PC[@]}" up -D >/dev/null 2>>"$_STACK_DIR/process-compose.log" \
  || err "process-compose did not start (log: $_STACK_DIR/process-compose.log)"
_DEADLINE=$((SECONDS + ${NANNOS_SLOT_HEALTH_TIMEOUT:-900}))
_LAST=""
while :; do
  _VERDICT="$(slot_pc_verdict "$_SLOT")"
  case "$_VERDICT" in
    ready) break ;;
    failed:*)
      err "Slot $_SLOT failed to start — ${_VERDICT#failed: } (logs: $_LOG_DIR; 'process-compose attach -u $_SOCK' shows the stack)" ;;
  esac
  if [[ $SECONDS -ge $_DEADLINE ]]; then
    err "Slot $_SLOT not ready after ${NANNOS_SLOT_HEALTH_TIMEOUT:-900}s — ${_VERDICT#starting: } (logs: $_LOG_DIR; 'process-compose attach -u $_SOCK' shows the stack)"
  fi
  if [[ "$_VERDICT" != "$_LAST" ]]; then
    log "Waiting for: ${_VERDICT#starting: }"
    _LAST="$_VERDICT"
  fi
  sleep 3
done
ok "Slot $_SLOT is up"

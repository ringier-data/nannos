# shellcheck shell=bash
# What a stack slot is (ADR-0016), defined once and sourced by slot.sh, start-local.sh and the
# setup steps of process-compose.yaml.

NANNOS_SLOTS_DIR="${NANNOS_SLOTS_DIR:-$HOME/.nannos/slots}"
PG_CONSOLE_CONTAINER="nannos-local-postgres-console"
PG_DOCSTORE_CONTAINER="nannos-local-postgres-docstore"

# Slot N owns the port block 4N000-4N999; these are the offsets in it. Slot 0 keeps the ports the
# stack has always had (adding N×100 to those would collide: slot 4's backend would be :5401).
SLOT_PORT_OFFSETS="backend=1 voice=2 runner=5 orchestrator=10 soffice=90 frontend=173 gateway=400
dbg_backend=678 dbg_orchestrator=679 dbg_runner=682 dbg_voice=683"
SLOT0_PORTS="backend=5001 voice=8002 runner=5005 orchestrator=10001 soffice=8090 frontend=5173 gateway=4000
dbg_backend=5678 dbg_orchestrator=5679 dbg_runner=5682 dbg_voice=5683"

slot_port() {  # N name
  local pair
  if [[ "$1" == 0 ]]; then
    for pair in $SLOT0_PORTS; do
      [[ "${pair%%=*}" == "$2" ]] && { echo "${pair#*=}"; return 0; }
    done
    return 1
  fi
  for pair in $SLOT_PORT_OFFSETS; do
    if [[ "${pair%%=*}" == "$2" ]]; then
      echo $((40000 + $1 * 1000 + ${pair#*=}))
      return 0
    fi
  done
  return 1
}

slot_all_ports() {  # N (1-8)
  local pair
  for pair in $SLOT_PORT_OFFSETS; do echo $((40000 + $1 * 1000 + ${pair#*=})); done
}

# A slot's directory: its claim, env, logs, gateway config and process-compose control socket.
# Slot 0 has one too (socket, stack.json), but no claim: it is whichever checkout runs start-local.
slot_dir() { echo "$NANNOS_SLOTS_DIR/$1"; }
# Kept short: a Unix socket path is limited to ~104 bytes on macOS.
slot_sock() { echo "$NANNOS_SLOTS_DIR/$1/pc.sock"; }

# Each database a slot has: logical name (slot 0's database), container, migrations dir.
SLOT_DB_SPECS="console:$PG_CONSOLE_CONTAINER:packages/console-backend/sqlmigrations/ddl
docstore:$PG_DOCSTORE_CONTAINER:packages/orchestrator-agent/sqlmigrations/ddl"

slot_db_name() {  # N logical-name
  if [[ "$1" == 0 ]]; then echo "$2"; else echo "${2}_s$1"; fi
}

slot_drop_databases() {  # N — never slot 0
  local spec logical container ddl
  [[ "$1" != 0 ]] || return 1
  for spec in $SLOT_DB_SPECS; do
    IFS=: read -r logical container ddl <<< "$spec"
    docker exec "$container" psql -U postgres -qc \
      "DROP DATABASE IF EXISTS \"$(slot_db_name "$1" "$logical")\" WITH (FORCE)" </dev/null >/dev/null || return 1
  done
  rm -f "$(slot_dir "$1")"/migrations-*.sha256
}

# Hashes of a worktree's migration files for one database: "<sha256>  <file>" per line.
slot_migration_hashes() {  # worktree-root ddl-dir
  (cd "$1/$2" && shasum -a 256 ./*.sql | sed "s|  \./|  |")
}

# process-compose against slot N's instance. The socket file outlives a killed instance, so
# "is it running" is whether the instance answers on it.
slot_pc() {  # N args...
  local n="$1"; shift
  process-compose "$@" -u "$(slot_sock "$n")" 2>/dev/null
}
slot_pc_running() {  # N
  [[ -S "$(slot_sock "$1")" ]] && slot_pc "$1" project state >/dev/null
}

# Stop whatever still runs of slot N's stack without its process-compose instance — after the
# instance was killed outright (SIGKILL, a crash), its services keep running as orphans, holding
# the slot's ports and its database connections. Every process of the stack inherits
# NANNOS_STACK_DIR, which `ps -E` shows for this user's processes; each is stopped by process group,
# as process-compose would have.
slot_kill_leftovers() {  # N
  local dir pgids pg
  dir="$(slot_dir "$1")"
  pgids="$(ps -axEww -o pgid=,command= 2>/dev/null \
    | awk -v want="NANNOS_STACK_DIR=$dir" -v self="$(ps -o pgid= -p $$ | tr -d ' ')" '
        { for (i = 2; i <= NF; i++) if ($i == want && $1 != self) { print $1; break } }' | sort -u)"
  [[ -n "$pgids" ]] || return 0
  for pg in $pgids; do kill -TERM -- "-$pg" 2>/dev/null || true; done
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    sleep 1
    for pg in $pgids; do kill -0 -- "-$pg" 2>/dev/null && continue 2; done
    return 0
  done
  for pg in $pgids; do kill -KILL -- "-$pg" 2>/dev/null || true; done
}

# Read slot N's process list and judge it: prints "ready", "starting" or "failed: <names>".
# Failed: a setup step exited non-zero or was skipped (its dependency failed), or a service exited.
# Ready: no failure, every setup step done and every service with a readiness probe ready.
slot_pc_verdict() {  # N
  slot_pc "$1" process list -o json | python3 -c '
import json, sys
try:
    procs = json.load(sys.stdin)
except ValueError:
    print("failed: (no answer)"); sys.exit()
failed, waiting = [], []
for p in procs:
    status, name = p["status"], p["name"]
    if status == "Disabled":
        continue
    one_shot = name in sys.argv[1].split()
    if status == "Skipped" or (status in ("Completed", "Error") and (not one_shot or p.get("exit_code") != 0)):
        failed.append(name)
    elif one_shot and status != "Completed":
        waiting.append(name)
    elif not one_shot and (status != "Running" or (p.get("has_ready_probe") and p.get("is_ready") != "Ready")):
        waiting.append(name)
if failed:
    print("failed: " + " ".join(sorted(failed)))
elif waiting:
    print("starting")
else:
    print("ready")
' "$SLOT_ONE_SHOTS"
}
# The setup steps of process-compose.yaml (run once, exit 0); everything else is a service.
SLOT_ONE_SHOTS="info infra migrate-console migrate-docstore keycloak-setup embed-sdk-build slack-migrate
deps-console-backend deps-orchestrator deps-runner deps-voice-agent deps-soffice-worker deps-frontend
deps-client-slack deps-client-slack-frontend deps-client-google-chat"

# A kernel lock (flock) on a file, held through file descriptor FD by the calling shell: the
# short Python child takes it on the shared open file, and it stays held until the shell closes
# FD or exits. A holder that dies releases it, so there is no stale lock to break.
slot_lock() {  # lock-file fd [timeout-seconds]
  mkdir -p "$(dirname "$1")"
  eval "exec $2>>\"\$1\""
  python3 -c '
import fcntl, sys, time
fd, deadline = int(sys.argv[1]), time.monotonic() + float(sys.argv[2])
while True:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sys.exit(0)
    except BlockingIOError:
        if time.monotonic() > deadline:
            sys.exit(1)
        time.sleep(0.1)
' "$2" "${3:-120}" || { echo "Timed out waiting for the lock $1" >&2; return 1; }
}

slot_unlock() {  # fd
  eval "exec $1>&-"
}

# Is the lock file held by someone? (Taken and released at once when it is not.)
slot_locked() {  # lock-file
  [[ -f "$1" ]] || return 1
  python3 -c '
import fcntl, sys
with open(sys.argv[1], "a") as f:
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)
sys.exit(1)
' "$1"
}

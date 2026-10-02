# shellcheck shell=bash
# What a stack slot is (ADR-0016), defined once and sourced by slot.sh and start-local.sh.

SLOT_COMMON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NANNOS_SLOTS_DIR="${NANNOS_SLOTS_DIR:-$HOME/.nannos/slots}"
PG_CONSOLE_CONTAINER="nannos-local-postgres-console"
PG_DOCSTORE_CONTAINER="nannos-local-postgres-docstore"

# Slot N owns the port block 4N000-4N999; these are the offsets in it.
SLOT_PORT_OFFSETS="backend=1 voice=2 runner=5 orchestrator=10 soffice=90 frontend=173 gateway=400
dbg_backend=678 dbg_orchestrator=679 dbg_runner=682 dbg_voice=683"

slot_port() {  # N name
  local pair
  for pair in $SLOT_PORT_OFFSETS; do
    if [[ "${pair%%=*}" == "$2" ]]; then
      echo $((40000 + $1 * 1000 + ${pair#*=}))
      return 0
    fi
  done
  return 1
}

slot_all_ports() {  # N
  local pair
  for pair in $SLOT_PORT_OFFSETS; do echo $((40000 + $1 * 1000 + ${pair#*=})); done
}

# Each database a slot has: logical name (slot 0's database), container, migrations dir.
SLOT_DB_SPECS="console:$PG_CONSOLE_CONTAINER:packages/console-backend/sqlmigrations/ddl
docstore:$PG_DOCSTORE_CONTAINER:packages/orchestrator-agent/sqlmigrations/ddl"

slot_db_name() {  # N logical-name
  if [[ "$1" == 0 ]]; then echo "$2"; else echo "${2}_s$1"; fi
}

# What outlives a slot's claim, for as long as its databases are kept: db-sN.owner (the
# worktree that created them), db-sN.sha256 (migration hashes), db-sN.from-slot0 (copy marker),
# db-sN.uploads/ (local file storage). Dropping the databases removes all of it.
slot_db_state() {  # N
  echo "$NANNOS_SLOTS_DIR/db-s$1"
}

slot_drop_databases() {  # N — never slot 0
  local spec logical container ddl
  [[ "$1" != 0 ]] || return 1
  for spec in $SLOT_DB_SPECS; do
    IFS=: read -r logical container ddl <<< "$spec"
    docker exec "$container" psql -U postgres -qc \
      "DROP DATABASE IF EXISTS \"$(slot_db_name "$1" "$logical")\" WITH (FORCE)" </dev/null >/dev/null || return 1
  done
  rm -rf "$(slot_db_state "$1")".*
}

# Hashes of a worktree's migration files: "<logical db> <sha256>  <file>" per line.
slot_migration_hashes() {  # worktree-root
  local spec logical container ddl
  for spec in $SLOT_DB_SPECS; do
    IFS=: read -r logical container ddl <<< "$spec"
    (cd "$1/$ddl" && shasum -a 256 ./*.sql | sed "s|  \./|  |; s|^|$logical |")
  done
}

slot_procs() {
  uv run --quiet --no-project --with pyyaml python "$SLOT_COMMON_DIR/slot_procs.py" "$@"
}

# A mkdir lock: atomic, no daemon. A holder that died leaves its pid behind and is broken.
slot_lock() {  # name
  local dir="$NANNOS_SLOTS_DIR/.$1.lock" holder i
  mkdir -p "$NANNOS_SLOTS_DIR"
  for i in $(seq 1 1200); do
    if mkdir "$dir" 2>/dev/null; then
      echo $$ > "$dir/pid"
      return 0
    fi
    holder="$(cat "$dir/pid" 2>/dev/null || true)"
    if [[ -n "$holder" ]] && ! kill -0 "$holder" 2>/dev/null; then
      rm -rf "$dir"
      continue
    fi
    sleep 0.1
  done
  echo "Timed out waiting for the $1 lock ($dir)" >&2
  return 1
}

slot_unlock() {  # name
  rm -rf "$NANNOS_SLOTS_DIR/.$1.lock"
}

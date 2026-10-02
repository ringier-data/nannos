#!/usr/bin/env bash
set -euo pipefail

# ─── Local stack slots (ADR-0016) ──────────────────────────────────
#
# Claims, starts, lists, stops and reclaims stacks that run side by side with the default
# stack (slot 0, `just start-local`) and with each other. A slot is a directory under
# $NANNOS_SLOTS_DIR (default ~/.nannos/slots): claiming one is an atomic mkdir.
#
#   slot.sh up [--slot N] [--local-idp] [--debug] [--from-slot0]
#                                                   claim a slot for this worktree, start it,
#                                                   print its JSON summary when healthy;
#                                                   --from-slot0 starts new databases as a
#                                                   copy of slot 0's
#   slot.sh down [N] [--keep-db]                    stop a slot and release it (default: this
#                                                   worktree's); drops its databases unless kept
#   slot.sh list                                    every claimed slot and its state
#   slot.sh gc                                      release every slot nothing runs in any more
#   slot.sh db-reset [N]                            down + drop databases + up again, same slot
#
# One worktree holds at most one slot; `up` from a worktree whose slot is running just prints it.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
SLOTS_DIR="${NANNOS_SLOTS_DIR:-$HOME/.nannos/slots}"
PG_CONSOLE_CONTAINER="nannos-local-postgres-console"
PG_DOCSTORE_CONTAINER="nannos-local-postgres-docstore"

err() { printf '✗ %s\n' "$*" >&2; exit 1; }
note() { printf '▸ %s\n' "$*" >&2; }

_procs() { uv run --quiet --no-project --with pyyaml python "$SCRIPT_DIR/slot_procs.py" "$@"; }

_claim_field() {  # slot_dir field
  python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' "$1/claim.json" "$2" 2>/dev/null || true
}

_pid_alive() { [[ -n "$1" ]] && kill -0 "$1" 2>/dev/null; }

# running: a service process is alive; starting: `up` is still working; dead: neither.
_state() {
  local dir="$SLOTS_DIR/$1"
  if [[ -f "$dir/pids.json" ]] && _procs status "$dir" >/dev/null 2>&1; then
    echo running
  elif _pid_alive "$(_claim_field "$dir" up_pid)"; then
    echo starting
  else
    echo dead
  fi
}

_slot_of_worktree() {
  local dir
  for dir in "$SLOTS_DIR"/[1-8]; do
    [[ -f "$dir/claim.json" ]] || continue
    if [[ "$(_claim_field "$dir" worktree)" == "$1" ]]; then
      basename "$dir"
      return
    fi
  done
}

_port_in_use() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

_psql() {  # container sql
  docker exec "$1" psql -U postgres -v ON_ERROR_STOP=1 -qc "$2" >/dev/null
}

cmd_up() {
  local want="" args=() n="" existing
  while [[ $# -gt 0 ]]; do
    case $1 in
      --slot) want="${2:-}"; shift 2 ;;
      --local-idp|--debug|--from-slot0) args+=("$1"); shift ;;
      *) err "Unknown flag for up: $1" ;;
    esac
  done
  [[ -z "$want" || "$want" =~ ^[1-8]$ ]] || err "--slot takes 1-8"
  mkdir -p "$SLOTS_DIR"
  "$SCRIPT_DIR/env-sync.sh" --quiet

  existing="$(_slot_of_worktree "$ROOT_DIR")"
  if [[ -n "$existing" ]]; then
    case "$(_state "$existing")" in
      running)
        note "This worktree already runs slot $existing"
        cat "$SLOTS_DIR/$existing/slot.json" 2>/dev/null && echo || _procs status "$SLOTS_DIR/$existing" >&2
        return 0 ;;
      starting)
        err "Slot $existing is still starting for this worktree (log: $SLOTS_DIR/$existing/up.log)" ;;
      dead)
        note "Slot $existing of this worktree is dead; restarting it on its databases"
        cmd_down "$existing" --keep-db
        want="$existing" ;;
    esac
  fi

  local candidates candidate base
  if [[ -n "$want" ]]; then candidates="$want"; else candidates="1 2 3 4 5 6 7 8"; fi
  for candidate in $candidates; do
    base=$((40000 + candidate * 1000))
    # Something outside the slot registry (a leftover process) may still hold the block.
    if _port_in_use $((base + 1)) || _port_in_use $((base + 173)); then
      continue
    fi
    if mkdir "$SLOTS_DIR/$candidate" 2>/dev/null; then
      n="$candidate"
      break
    fi
  done
  [[ -n "$n" ]] || err "No free slot${want:+ ($want is taken)}. See 'just slots'; 'just slots-gc' releases dead ones."

  python3 -c '
import json, sys, time
slot, worktree, up_pid, *args = sys.argv[1:]
print(json.dumps({"slot": int(slot), "worktree": worktree, "up_pid": int(up_pid), "args": args,
                  "claimed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, indent=2))
' "$n" "$ROOT_DIR" "$$" ${args[@]+"${args[@]}"} > "$SLOTS_DIR/$n/claim.json"

  note "Starting slot $n for $ROOT_DIR (log: $SLOTS_DIR/$n/up.log)"
  if "$ROOT_DIR/scripts/start-local.sh" --slot "$n" --headless ${args[@]+"${args[@]}"} > "$SLOTS_DIR/$n/up.log" 2>&1; then
    cat "$SLOTS_DIR/$n/slot.json"
    echo
  else
    tail -25 "$SLOTS_DIR/$n/up.log" >&2
    err "Slot $n did not come up. Full log: $SLOTS_DIR/$n/up.log — 'just down $n' releases it."
  fi
}

cmd_down() {
  local n="" keep_db=""
  while [[ $# -gt 0 ]]; do
    case $1 in
      --keep-db) keep_db=1; shift ;;
      [1-8]) n="$1"; shift ;;
      *) err "Unknown argument for down: $1" ;;
    esac
  done
  [[ -n "$n" ]] || n="$(_slot_of_worktree "$ROOT_DIR")"
  [[ -n "$n" ]] || err "This worktree holds no slot. Name one: 'just down N' (see 'just slots')."
  local dir="$SLOTS_DIR/$n"
  [[ -d "$dir" ]] || err "Slot $n is not claimed"
  if [[ "$(_state "$n")" == starting ]]; then
    err "Slot $n is still starting (pid $(_claim_field "$dir" up_pid)); stop that first"
  fi

  [[ -f "$dir/pids.json" ]] && _procs stop "$dir" >&2
  docker rm -f "nannos-gw-s$n" >/dev/null 2>&1 || true
  if [[ -z "$keep_db" ]]; then
    _psql "$PG_CONSOLE_CONTAINER" "DROP DATABASE IF EXISTS \"console_s$n\" WITH (FORCE)" \
      || note "Could not drop console_s$n (is $PG_CONSOLE_CONTAINER running?)"
    _psql "$PG_DOCSTORE_CONTAINER" "DROP DATABASE IF EXISTS \"docstore_s$n\" WITH (FORCE)" \
      || note "Could not drop docstore_s$n (is $PG_DOCSTORE_CONTAINER running?)"
    rm -f "$SLOTS_DIR/db-s$n.sha256" "$SLOTS_DIR/db-s$n.from-slot0"
  fi
  rm -rf "$dir"
  note "Slot $n released${keep_db:+ (databases kept)}"
}

cmd_list() {
  local dir n found=""
  printf '%-5s %-9s %-24s %s\n' SLOT STATE CONSOLE WORKTREE
  for dir in "$SLOTS_DIR"/[1-8]; do
    [[ -d "$dir" ]] || continue
    found=1
    n="$(basename "$dir")"
    printf '%-5s %-9s %-24s %s\n' "$n" "$(_state "$n")" "http://localhost:$((40000 + n * 1000 + 173))" "$(_claim_field "$dir" worktree)"
  done
  [[ -n "$found" ]] || printf '(no slots claimed; slot 0 is `just start-local`)\n'
}

cmd_gc() {
  local dir n
  for dir in "$SLOTS_DIR"/[1-8]; do
    [[ -d "$dir" ]] || continue
    n="$(basename "$dir")"
    if [[ "$(_state "$n")" == dead ]]; then
      cmd_down "$n"
    fi
  done
}

cmd_db_reset() {
  local n="${1:-}"
  [[ -n "$n" ]] || n="$(_slot_of_worktree "$ROOT_DIR")"
  [[ -n "$n" ]] || err "This worktree holds no slot. Name one: 'just db-reset N'."
  local dir="$SLOTS_DIR/$n" worktree args
  [[ -d "$dir" ]] || err "Slot $n is not claimed"
  worktree="$(_claim_field "$dir" worktree)"
  args="$(python3 -c 'import json, sys; print(" ".join(json.load(open(sys.argv[1])).get("args", [])))' "$dir/claim.json")"
  cmd_down "$n"
  # shellcheck disable=SC2086 # args are our own flags, no spaces
  "$worktree/scripts/local-dev/slot.sh" up --slot "$n" $args
}

case "${1:-}" in
  up) shift; cmd_up "$@" ;;
  down) shift; cmd_down "$@" ;;
  list) shift; cmd_list ;;
  gc) shift; cmd_gc ;;
  db-reset) shift; cmd_db_reset "$@" ;;
  *) sed -n '4,20p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac

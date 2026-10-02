#!/usr/bin/env bash
set -euo pipefail

# ─── Local stack slots (ADR-0016) ──────────────────────────────────
#
# Claims, starts, lists, stops and reclaims stacks that run side by side with the default
# stack (slot 0, `just start-local`) and with each other. A slot is a directory under
# $NANNOS_SLOTS_DIR (default ~/.nannos/slots); claims are taken under one lock.
#
#   slot.sh up [--slot N] [--local-idp] [--debug] [--from-slot0]
#                                                   claim a slot for this worktree, start it,
#                                                   print its JSON summary when healthy;
#                                                   --from-slot0 starts new databases as a
#                                                   copy of slot 0's
#   slot.sh down [N] [--keep-db]                    stop a slot and release it (default: this
#                                                   worktree's); drops its databases unless kept
#   slot.sh list                                    every slot: claimed, or holding kept databases
#   slot.sh gc                                      release every slot nothing runs in any more
#   slot.sh db-reset [N]                            down + drop databases + up again, same slot
#
# One worktree holds at most one slot; `up` from a worktree whose slot is running just prints it.
# Kept databases belong to the worktree that created them: no other worktree's `up` takes them.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=slot-common.sh
source "$SCRIPT_DIR/slot-common.sh"
SLOTS_DIR="$NANNOS_SLOTS_DIR"

err() { printf '✗ %s\n' "$*" >&2; exit 1; }
note() { printf '▸ %s\n' "$*" >&2; }

_claim_field() {  # slot_dir field
  python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' "$1/claim.json" "$2" 2>/dev/null || true
}

_pid_alive() { [[ -n "$1" ]] && kill -0 "$1" 2>/dev/null; }

# running: a service process is alive; starting: `up` is still working; dead: neither.
_state() {
  local dir="$SLOTS_DIR/$1"
  if [[ -f "$dir/pids.json" ]] && slot_procs status "$dir" >/dev/null 2>&1; then
    echo running
  elif _pid_alive "$(_claim_field "$dir" up_pid)"; then
    echo starting
  else
    echo dead
  fi
}

# Healthy is what `up` waited for: every service process alive and the backend answering.
_healthy() {
  [[ -f "$SLOTS_DIR/$1/slot.json" ]] \
    && slot_procs status "$SLOTS_DIR/$1" --all >/dev/null 2>&1 \
    && [[ "$(curl -s -o /dev/null --max-time 3 -w '%{http_code}' "http://localhost:$(slot_port "$1" backend)/api/v1/health")" == 200 ]]
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

_kept_owner() { cat "$(slot_db_state "$1").owner" 2>/dev/null || true; }

_port_in_use() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

_block_in_use() {  # N: is any port of the slot's block held by something?
  local port
  for port in $(slot_all_ports "$1"); do
    _port_in_use "$port" && return 0
  done
  return 1
}

cmd_up() {
  local want="" args=() n="" existing candidate owner
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

  # Finding this worktree's slot and claiming a new one happen under one lock, so two `up`s
  # from one worktree cannot both claim, and `gc` never sees a claim half-written.
  slot_lock claim
  existing="$(_slot_of_worktree "$ROOT_DIR")"
  if [[ -n "$existing" ]]; then
    case "$(_state "$existing")" in
      running)
        slot_unlock claim
        _healthy "$existing" \
          || err "Slot $existing of this worktree runs but is not healthy (logs: $SLOTS_DIR/$existing/logs). 'just down' and 'just up' restart it."
        note "This worktree already runs slot $existing"
        cat "$SLOTS_DIR/$existing/slot.json"
        echo
        return 0 ;;
      starting)
        slot_unlock claim
        err "Slot $existing is still starting for this worktree (log: $SLOTS_DIR/$existing/up.log)" ;;
      dead)
        slot_unlock claim
        note "Slot $existing of this worktree is dead; restarting it on its databases"
        cmd_down "$existing" --keep-db
        slot_lock claim
        want="$existing" ;;
    esac
  fi

  local candidates
  if [[ -n "$want" ]]; then candidates="$want"; else candidates="1 2 3 4 5 6 7 8"; fi
  for candidate in $candidates; do
    [[ ! -d "$SLOTS_DIR/$candidate" ]] || continue
    owner="$(_kept_owner "$candidate")"
    if [[ -n "$owner" && "$owner" != "$ROOT_DIR" ]]; then
      [[ -z "$want" ]] || { slot_unlock claim; err "Slot $candidate keeps the databases of $owner. 'just down $candidate' drops them."; }
      continue
    fi
    # Something outside the slot registry (a leftover process) may still hold the block.
    _block_in_use "$candidate" && continue
    mkdir "$SLOTS_DIR/$candidate"
    n="$candidate"
    break
  done
  if [[ -z "$n" ]]; then
    slot_unlock claim
    err "No free slot${want:+ ($want is taken)}. See 'just slots'; 'just slots-gc' releases dead ones."
  fi

  python3 -c '
import json, sys, time
slot, worktree, up_pid, *args = sys.argv[1:]
print(json.dumps({"slot": int(slot), "worktree": worktree, "up_pid": int(up_pid), "args": args,
                  "claimed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, indent=2))
' "$n" "$ROOT_DIR" "$$" ${args[@]+"${args[@]}"} > "$SLOTS_DIR/$n/claim.json"
  slot_unlock claim

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
  local dir="$SLOTS_DIR/$n" owner
  owner="$(_kept_owner "$n")"
  [[ -d "$dir" || -n "$owner" ]] || err "Slot $n is neither claimed nor keeping databases"
  if [[ -d "$dir" && "$(_state "$n")" == starting ]]; then
    err "Slot $n is still starting (pid $(_claim_field "$dir" up_pid)); stop that first"
  fi

  [[ -f "$dir/pids.json" ]] && slot_procs stop "$dir" >&2
  docker rm -f "nannos-gw-s$n" >/dev/null 2>&1 || true
  if [[ -z "$keep_db" ]]; then
    slot_drop_databases "$n" \
      || note "Could not drop slot $n's databases (are $PG_CONSOLE_CONTAINER and $PG_DOCSTORE_CONTAINER running?)"
  fi
  rm -rf "$dir"
  local kept=""
  [[ -z "$keep_db" ]] || kept=" (databases kept${owner:+ for $owner})"
  note "Slot $n released$kept"
}

cmd_list() {
  local n found="" state worktree
  printf '%-5s %-9s %-24s %s\n' SLOT STATE CONSOLE WORKTREE
  for n in 1 2 3 4 5 6 7 8; do
    if [[ -d "$SLOTS_DIR/$n" ]]; then
      state="$(_state "$n")"
      worktree="$(_claim_field "$SLOTS_DIR/$n" worktree)"
    elif [[ -n "$(_kept_owner "$n")" ]]; then
      state="kept-db"
      worktree="$(_kept_owner "$n")"
    else
      continue
    fi
    found=1
    printf '%-5s %-9s %-24s %s\n' "$n" "$state" "http://localhost:$(slot_port "$n" frontend)" "$worktree"
  done
  [[ -n "$found" ]] || printf '(no slots claimed; slot 0 is `just start-local`)\n'
}

cmd_gc() {
  local dir n dead=()
  slot_lock claim
  for dir in "$SLOTS_DIR"/[1-8]; do
    [[ -d "$dir" ]] || continue
    n="$(basename "$dir")"
    [[ "$(_state "$n")" == dead ]] && dead+=("$n")
  done
  slot_unlock claim
  for n in ${dead[@]+"${dead[@]}"}; do
    cmd_down "$n"
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
  *) sed -n '4,22p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac

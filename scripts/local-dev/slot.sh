#!/usr/bin/env bash
set -euo pipefail

# ─── Local stack slots (ADR-0016) ──────────────────────────────────
#
# Claims, starts, lists, stops and reclaims stacks that run side by side with the default
# stack (slot 0, `just start-local`) and with each other. A slot is a directory under
# $NANNOS_SLOTS_DIR (default ~/.nannos/slots) holding its claim, env, logs and the control socket
# of its process-compose instance; claims are taken under one lock.
#
#   slot.sh up [--slot N] [--local-idp] [--debug]   claim a slot for this worktree, start it,
#                                                   print its JSON summary when ready
#   slot.sh restart [N] [--local-idp] [--debug]     stop and start again: same claim, same
#                                                   databases, pending migrations applied;
#                                                   flags given replace the slot's flags
#   slot.sh down [N]                                stop a slot, drop its databases, release it
#   slot.sh list                                    every running stack, slot 0 included
#   slot.sh gc                                      release every slot whose instance is gone
#   slot.sh db-reset [N]                            drop the databases and start again
#
# N defaults to this worktree's slot. One worktree holds at most one slot; `up` from a worktree
# whose slot is running just prints it — or, given other flags than the slot runs with, restarts it
# with those.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=slot-common.sh
source "$SCRIPT_DIR/slot-common.sh"
SLOTS_DIR="$NANNOS_SLOTS_DIR"
CLAIM_LOCK="$SLOTS_DIR/.claim.flock"

err() { printf '✗ %s\n' "$*" >&2; exit 1; }
note() { printf '▸ %s\n' "$*" >&2; }

command -v process-compose >/dev/null 2>&1 || err "process-compose is missing: brew install f1bonacc1/tap/process-compose"

_claim_field() {  # N field
  python3 -c 'import json, sys; v = json.load(open(sys.argv[1])).get(sys.argv[2], ""); print(" ".join(v) if isinstance(v, list) else v)' \
    "$SLOTS_DIR/$1/claim.json" "$2" 2>/dev/null || true
}

# starting: an `up` or `restart` holds the slot's start lock; running / failed: its instance
# answers, and is ready or has a failed process; dead: claimed, but no instance answers.
_state() {  # N
  if slot_locked "$SLOTS_DIR/$1/start.flock"; then
    echo starting
  elif slot_pc_running "$1"; then
    case "$(slot_pc_verdict "$1")" in
      ready) echo running ;;
      starting) echo starting ;;
      *) echo failed ;;
    esac
  else
    echo dead
  fi
}

_slot_of_worktree() {
  local dir
  for dir in "$SLOTS_DIR"/[1-8]; do
    [[ -f "$dir/claim.json" ]] || continue
    if [[ "$(_claim_field "$(basename "$dir")" worktree)" == "$1" ]]; then
      basename "$dir"
      return
    fi
  done
}

_slot_arg() {  # [N]: the named slot, else this worktree's
  local n="${1:-}"
  [[ -z "$n" || "$n" =~ ^[1-8]$ ]] || err "A slot is 1-8 (slot 0 is 'just start-local')"
  [[ -n "$n" ]] || n="$(_slot_of_worktree "$ROOT_DIR")"
  [[ -n "$n" ]] || err "This worktree holds no slot. Name one (see 'just slots')."
  [[ -f "$SLOTS_DIR/$n/claim.json" ]] || err "Slot $n is not claimed"
  echo "$n"
}

_port_in_use() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

_block_in_use() {  # N: is any port of the slot's block held by something?
  local port
  for port in $(slot_all_ports "$1"); do
    _port_in_use "$port" && return 0
  done
  return 1
}

_set_args() {  # N flags... — the flags the slot starts with from now on
  python3 -c '
import json, sys
path, args = sys.argv[1], sys.argv[2:]
claim = json.load(open(path))
claim["args"] = args
json.dump(claim, open(path, "w"), indent=2)
' "$SLOTS_DIR/$1/claim.json" "${@:2}"
}

# The same flags, whatever their order.
_same_args() {  # N flags...
  [[ "$(printf '%s\n' "$(_claim_field "$1" args)" | tr ' ' '\n' | grep . | sort | tr '\n' ' ')" \
     == "$(printf '%s\n' "${@:2}" | grep . | sort | tr '\n' ' ')" ]]
}

# Start slot N's stack (claimed, not running) and wait until it is ready. The start lock tells
# everyone else it is starting; the kernel drops it when this shell ends, however it ends.
_start() {  # N
  local n="$1" dir="$SLOTS_DIR/$1" args
  args="$(_claim_field "$n" args)"
  slot_lock "$dir/start.flock" 7 1 || err "Slot $n is already starting"
  note "Starting slot $n for $(_claim_field "$n" worktree) (log: $dir/up.log)"
  # The lock stays with this shell (7>&-): the stack's process-compose instance, started by
  # start-local.sh, would otherwise inherit it and keep the slot "starting" for as long as it runs.
  # shellcheck disable=SC2086 # args are our own flags, no spaces
  if "$(_claim_field "$n" worktree)/scripts/start-local.sh" --slot "$n" --headless $args > "$dir/up.log" 2>&1 7>&-; then
    slot_unlock 7
    cat "$dir/stack.json"
    echo
  else
    slot_unlock 7
    tail -25 "$dir/up.log" >&2
    err "Slot $n did not come up. Full log: $dir/up.log; logs: $dir/logs. 'just restart' tries again, 'just down' releases it."
  fi
}

# Stop slot N's stack, anything left of it and its gateway container; keeps the claim and the
# databases.
_stop() {  # N
  local n="$1"
  if slot_pc_running "$n"; then
    slot_pc "$n" down >/dev/null || true
    # `down` returns once it asked; wait for the instance to be gone.
    for _ in $(seq 1 60); do slot_pc_running "$n" || break; sleep 1; done
  fi
  slot_kill_leftovers "$n"
  docker rm -f "nannos-gw-s$n" >/dev/null 2>&1 || true
  rm -f "$(slot_sock "$n")"
}

cmd_up() {
  local want="" args=() n="" existing candidate
  while [[ $# -gt 0 ]]; do
    case $1 in
      --slot) want="${2:-}"; shift 2 ;;
      --local-idp|--debug) args+=("$1"); shift ;;
      *) err "Unknown flag for up: $1" ;;
    esac
  done
  [[ -z "$want" || "$want" =~ ^[1-8]$ ]] || err "--slot takes 1-8"
  mkdir -p "$SLOTS_DIR"
  "$SCRIPT_DIR/env-sync.sh" --quiet

  # Finding this worktree's slot and claiming a new one happen under one lock, so two `up`s
  # from one worktree cannot both claim, and `gc` never sees a claim half-written.
  slot_lock "$CLAIM_LOCK" 8
  existing="$(_slot_of_worktree "$ROOT_DIR")"
  if [[ -n "$existing" ]]; then
    slot_unlock 8
    # Flags given that differ from the slot's: they would otherwise be ignored without a word
    # (a slot started without --local-idp kept signing in at the remote IdP).
    if [[ ${#args[@]} -gt 0 ]] && ! _same_args "$existing" "${args[@]}"; then
      [[ "$(_state "$existing")" != starting ]] || err "Slot $existing is still starting for this worktree (log: $SLOTS_DIR/$existing/up.log)"
      note "Slot $existing runs with '$(_claim_field "$existing" args)'; restarting it with '${args[*]}' on its databases"
      _set_args "$existing" "${args[@]}"
      _stop "$existing"
      _start "$existing"
      return
    fi
    case "$(_state "$existing")" in
      running)
        note "This worktree already runs slot $existing"
        cat "$SLOTS_DIR/$existing/stack.json"
        echo
        return 0 ;;
      starting)
        err "Slot $existing is still starting for this worktree (log: $SLOTS_DIR/$existing/up.log)" ;;
      failed)
        err "Slot $existing of this worktree runs, but $(slot_pc_verdict "$existing" | sed 's/^failed: //') failed (logs: $SLOTS_DIR/$existing/logs). Fix it and 'just restart'." ;;
      dead)
        note "Slot $existing of this worktree is not running; starting it again on its databases"
        _stop "$existing"
        _start "$existing"
        return ;;
    esac
  fi

  local candidates
  if [[ -n "$want" ]]; then candidates="$want"; else candidates="1 2 3 4 5 6 7 8"; fi
  for candidate in $candidates; do
    [[ ! -d "$SLOTS_DIR/$candidate" ]] || continue
    # Something outside the slot registry (a leftover process) may still hold the block.
    _block_in_use "$candidate" && continue
    mkdir "$SLOTS_DIR/$candidate"
    n="$candidate"
    break
  done
  if [[ -z "$n" ]]; then
    slot_unlock 8
    err "No free slot${want:+ ($want is taken or its ports are in use)}. See 'just slots'; 'just slots-gc' releases dead ones."
  fi
  python3 -c '
import json, sys, time
slot, worktree, *args = sys.argv[1:]
print(json.dumps({"slot": int(slot), "worktree": worktree, "args": args,
                  "claimed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, indent=2))
' "$n" "$ROOT_DIR" ${args[@]+"${args[@]}"} > "$SLOTS_DIR/$n/claim.json"
  slot_unlock 8
  _start "$n"
}

cmd_restart() {
  local n="" args=()
  while [[ $# -gt 0 ]]; do
    case $1 in
      --local-idp|--debug) args+=("$1"); shift ;;
      *) [[ -z "$n" ]] || err "Unknown argument for restart: $1"; n="$1"; shift ;;
    esac
  done
  n="$(_slot_arg "$n")"
  [[ "$(_state "$n")" != starting ]] || err "Slot $n is still starting (log: $SLOTS_DIR/$n/up.log)"
  [[ ${#args[@]} -eq 0 ]] || _set_args "$n" "${args[@]}"
  _stop "$n"
  _start "$n"
}

cmd_down() {
  local n
  n="$(_slot_arg "${1:-}")"
  [[ "$(_state "$n")" != starting ]] || err "Slot $n is still starting (log: $SLOTS_DIR/$n/up.log); stop that first"
  _stop "$n"
  slot_drop_databases "$n" \
    || err "Could not drop slot $n's databases (are $PG_CONSOLE_CONTAINER and $PG_DOCSTORE_CONTAINER running?); the slot stays claimed"
  rm -rf "${SLOTS_DIR:?}/$n"
  note "Slot $n released"
}

cmd_db_reset() {
  local n
  n="$(_slot_arg "${1:-}")"
  [[ "$(_state "$n")" != starting ]] || err "Slot $n is still starting (log: $SLOTS_DIR/$n/up.log)"
  _stop "$n"
  slot_drop_databases "$n" || err "Could not drop slot $n's databases"
  rm -rf "$SLOTS_DIR/$n/uploads"
  _start "$n"
}

# Slot 0 has no claim: it is whichever checkout runs start-local. Its instance names that checkout
# in stack.json; a slot 0 started before process-compose is found by the checkout of the process
# holding its backend port — when that is a git checkout at all (a Docker port is not).
_slot0_worktree() {
  local pid cwd
  if slot_pc_running 0; then
    python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["worktree"])' "$SLOTS_DIR/0/stack.json" 2>/dev/null && return
  fi
  pid="$(lsof -nP -iTCP:5001 -sTCP:LISTEN -t 2>/dev/null | head -1 || true)"
  [[ -n "$pid" ]] || return 0
  cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' || true)"
  [[ -n "$cwd" ]] || return 0
  git -C "$cwd" rev-parse --show-toplevel 2>/dev/null || true
}

_branch_of() {  # worktree
  [[ -d "$1" ]] || { echo "(gone)"; return; }
  git -C "$1" branch --show-current 2>/dev/null | grep . || echo "(detached)"
}

cmd_list() {
  local n slot0
  local row='%-5s %-9s %-24s %-36s %s\n'
  printf "$row" SLOT STATE CONSOLE BRANCH WORKTREE
  slot0="$(_slot0_worktree)"
  if [[ -n "$slot0" ]]; then
    printf "$row" 0 running "http://localhost:5173" "$(_branch_of "$slot0")" "$slot0"
  fi
  for n in 1 2 3 4 5 6 7 8; do
    [[ -f "$SLOTS_DIR/$n/claim.json" ]] || continue
    printf "$row" "$n" "$(_state "$n")" "http://localhost:$(slot_port "$n" frontend)" \
      "$(_branch_of "$(_claim_field "$n" worktree)")" "$(_claim_field "$n" worktree)"
  done
}

cmd_gc() {
  local n
  # Under the claim lock throughout: a slot found dead is released before anyone can claim it.
  slot_lock "$CLAIM_LOCK" 8
  for n in 1 2 3 4 5 6 7 8; do
    [[ -d "$SLOTS_DIR/$n" ]] || continue
    if [[ ! -f "$SLOTS_DIR/$n/claim.json" ]] || [[ "$(_state "$n")" == dead ]]; then
      _stop "$n"
      if slot_drop_databases "$n"; then
        rm -rf "${SLOTS_DIR:?}/$n"
        note "Slot $n released"
      else
        note "Could not drop slot $n's databases; it stays claimed"
      fi
    fi
  done
  slot_unlock 8
}

# The control socket of slot N (0-8), else this worktree's slot, else slot 0.
cmd_sock() {
  local n="${1:-}"
  [[ -z "$n" || "$n" =~ ^[0-8]$ ]] || err "A slot is 0-8"
  [[ -n "$n" ]] || n="$(_slot_of_worktree "$ROOT_DIR")"
  slot_sock "${n:-0}"
}

case "${1:-}" in
  up) shift; cmd_up "$@" ;;
  sock) shift; cmd_sock "$@" ;;
  restart) shift; cmd_restart "$@" ;;
  down) shift; cmd_down "$@" ;;
  list) shift; cmd_list ;;
  gc) shift; cmd_gc ;;
  db-reset) shift; cmd_db_reset "$@" ;;
  *) sed -n '4,22p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac

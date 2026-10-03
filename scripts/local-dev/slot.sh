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
#                                                   print its JSON summary when ready;
#                                                   --remote-idp / --no-debug remove a flag
#   slot.sh restart [N] [flags]                     stop and start again: same claim, same
#                                                   databases, pending migrations applied;
#                                                   flags given change the slot's (as for up)
#   slot.sh down [N]                                stop a slot, drop its databases, release it
#   slot.sh list                                    every running stack, slot 0 included
#   slot.sh gc                                      release every slot whose instance is gone
#                                                   (not the ones stop-local stopped)
#   slot.sh stop-all | down-all                     for stop-local / reset-local
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

# Starting a stack needs process-compose; stopping and releasing work without it (no instance
# can run then), so stop-local / reset-local never stop half-way on a machine that lacks it.
_need_pc() {
  command -v process-compose >/dev/null 2>&1 || err "process-compose is missing: brew install f1bonacc1/tap/process-compose"
}

_claim_field() {  # N field
  python3 -c 'import json, sys; v = json.load(open(sys.argv[1])).get(sys.argv[2], ""); print(" ".join(v) if isinstance(v, list) else v)' \
    "$SLOTS_DIR/$1/claim.json" "$2" 2>/dev/null || true
}

# starting: an `up` or `restart` holds the slot's start lock; running / failed: its instance
# answers, and is ready or has a failed process; stopped: `stop-local` stopped it on purpose
# (kept, and left alone by `slots-gc`); dead: claimed, but no instance answers.
_state() {  # N
  if slot_locked "$SLOTS_DIR/$1/start.flock"; then
    echo starting
  elif slot_pc_running "$1"; then
    case "$(slot_pc_verdict "$1")" in
      ready) echo running ;;
      starting*) echo starting ;;
      *) echo failed ;;
    esac
  elif [[ -f "$SLOTS_DIR/$1/stopped" ]]; then
    echo stopped
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

_block_in_use() {  # N: is any port of the slot's block held by something? Prints the first.
  local port
  for port in $(slot_all_ports "$1"); do
    _port_in_use "$port" && { echo "$port"; return 0; }
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

# The slot's flags changed by the ones given: --local-idp / --debug add, --remote-idp / --no-debug
# remove, anything not named stays. Prints one per line.
_merged_args() {  # N flags...
  python3 -c '
import sys
current, given = sys.argv[1].split(), sys.argv[2:]
off = {"--remote-idp": "--local-idp", "--no-debug": "--debug"}
args = [a for a in current if a not in {off[g] for g in given if g in off}]
args += [g for g in given if g not in off and g not in args]
print("\n".join(args))
' "$(_claim_field "$1" args)" "${@:2}"
}

# Take slot N's start lock, held through FD 7 until _start is done. It tells everyone else the
# slot is starting — `gc` and another `up` leave it alone, also while _stop runs before _start —
# and the kernel drops it when this shell ends, however it ends.
_begin() {  # N
  slot_lock "$SLOTS_DIR/$1/start.flock" 7 1 || err "Slot $1 is already starting or stopping (log: $SLOTS_DIR/$1/up.log)"
}

# Start slot N's stack (claimed, not running; _begin first) and wait until it is ready.
_start() {  # N
  local n="$1" dir="$SLOTS_DIR/$1" args worktree port
  args="$(_claim_field "$n" args)"
  worktree="$(_claim_field "$n" worktree)"
  [[ -n "$worktree" && -x "$worktree/scripts/start-local.sh" ]] \
    || { slot_unlock 7; err "Slot $n's claim names no usable worktree ('$worktree'); 'just down $n' releases it"; }
  # Ports are checked when a slot is claimed; by a restart, something else may have taken one.
  if port="$(_block_in_use "$n")"; then
    slot_unlock 7
    err "Port $port of slot $n's block is held by another process ('lsof -nP -iTCP:$port -sTCP:LISTEN' names it)"
  fi
  note "Starting slot $n for $worktree (log: $dir/up.log)"
  # The lock stays with this shell (7>&-): the stack's process-compose instance, started by
  # start-local.sh, would otherwise inherit it and keep the slot "starting" for as long as it runs.
  # shellcheck disable=SC2086 # args are our own flags, no spaces
  if "$worktree/scripts/start-local.sh" --slot "$n" --headless $args > "$dir/up.log" 2>&1 7>&-; then
    # Only now: a start that fails early (an expired SSO session, Docker down) leaves no instance,
    # and a slot stopped on purpose must not then read as dead to gc.
    rm -f "$dir/stopped"
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

# Stop slot N, drop its databases and remove its directory (start lock held: _begin).
_release() {  # N
  _stop "$1"
  if ! slot_drop_databases "$1"; then
    slot_unlock 7
    note "Could not drop slot $1's databases (are $PG_CONSOLE_CONTAINER and $PG_DOCSTORE_CONTAINER running?); the slot stays claimed"
    return 1
  fi
  rm -rf "${SLOTS_DIR:?}/$1"
  slot_unlock 7
  note "Slot $1 released"
}

cmd_up() {
  local want="" flags=() n="" existing candidate state new_args=() a
  while [[ $# -gt 0 ]]; do
    case $1 in
      --slot) want="${2:-}"; shift 2 ;;
      --local-idp|--debug|--remote-idp|--no-debug) flags+=("$1"); shift ;;
      *) err "Unknown flag for up: $1" ;;
    esac
  done
  [[ -z "$want" || "$want" =~ ^[1-8]$ ]] || err "--slot takes 1-8"
  _need_pc
  mkdir -p "$SLOTS_DIR"
  "$SCRIPT_DIR/env-sync.sh" --quiet

  # Finding this worktree's slot and claiming a new one happen under one lock, so two `up`s
  # from one worktree cannot both claim, and `gc` never sees a claim half-written. A slot that
  # is (re)started takes its start lock before the claim lock is let go: `gc` cannot slip in.
  slot_lock "$CLAIM_LOCK" 8
  existing="$(_slot_of_worktree "$ROOT_DIR")"
  if [[ -n "$existing" ]]; then
    [[ -z "$want" || "$want" == "$existing" ]] \
      || { slot_unlock 8; err "This worktree already holds slot $existing; 'just down' releases it before 'just up --slot $want'"; }
    state="$(_state "$existing")"
    [[ "$state" != starting ]] || { slot_unlock 8; err "Slot $existing is still starting for this worktree (log: $SLOTS_DIR/$existing/up.log)"; }
    # Flags that change the slot's: they would otherwise be ignored without a word (a slot
    # started without --local-idp kept signing in at the remote IdP).
    if [[ ${#flags[@]} -gt 0 ]]; then
      while IFS= read -r a; do [[ -n "$a" ]] && new_args+=("$a"); done < <(_merged_args "$existing" "${flags[@]}")
      if [[ "${new_args[*]+${new_args[*]}}" != "$(_claim_field "$existing" args)" ]]; then
        _begin "$existing"
        slot_unlock 8
        note "Slot $existing runs with '$(_claim_field "$existing" args)'; restarting it with '${new_args[*]+${new_args[*]}}' on its databases"
        _set_args "$existing" ${new_args[@]+"${new_args[@]}"}
        _stop "$existing"
        _start "$existing"
        return
      fi
    fi
    case "$state" in
      running)
        slot_unlock 8
        note "This worktree already runs slot $existing"
        cat "$SLOTS_DIR/$existing/stack.json"
        echo
        return 0 ;;
      failed)
        slot_unlock 8
        err "Slot $existing of this worktree runs, but $(slot_pc_verdict "$existing" | sed 's/^failed: //') failed (logs: $SLOTS_DIR/$existing/logs). Fix it and 'just restart'." ;;
      *)
        _begin "$existing"
        slot_unlock 8
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
    _block_in_use "$candidate" >/dev/null && continue
    mkdir "$SLOTS_DIR/$candidate"
    n="$candidate"
    break
  done
  if [[ -z "$n" ]]; then
    slot_unlock 8
    err "No free slot${want:+ ($want is taken or its ports are in use)}. See 'just slots'; 'just slots-gc' releases dead ones."
  fi
  for a in ${flags[@]+"${flags[@]}"}; do
    case "$a" in --local-idp|--debug) new_args+=("$a") ;; esac
  done
  python3 -c '
import json, sys, time
slot, worktree, *args = sys.argv[1:]
print(json.dumps({"slot": int(slot), "worktree": worktree, "args": args,
                  "claimed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, indent=2))
' "$n" "$ROOT_DIR" ${new_args[@]+"${new_args[@]}"} > "$SLOTS_DIR/$n/claim.json"
  _begin "$n"
  slot_unlock 8
  _start "$n"
}

cmd_restart() {
  local n="" flags=() new_args=() a
  while [[ $# -gt 0 ]]; do
    case $1 in
      --local-idp|--debug|--remote-idp|--no-debug) flags+=("$1"); shift ;;
      *) [[ -z "$n" ]] || err "Unknown argument for restart: $1"; n="$1"; shift ;;
    esac
  done
  _need_pc
  n="$(_slot_arg "$n")"
  # Start lock taken under the claim lock, as in `up`: `gc` decides under the claim lock.
  slot_lock "$CLAIM_LOCK" 8
  _begin "$n"
  slot_unlock 8
  if [[ ${#flags[@]} -gt 0 ]]; then
    while IFS= read -r a; do [[ -n "$a" ]] && new_args+=("$a"); done < <(_merged_args "$n" "${flags[@]}")
    _set_args "$n" ${new_args[@]+"${new_args[@]}"}
  fi
  _stop "$n"
  _start "$n"
}

cmd_down() {
  local n
  n="$(_slot_arg "${1:-}")"
  slot_lock "$CLAIM_LOCK" 8
  _begin "$n"
  slot_unlock 8
  _release "$n"
}

cmd_db_reset() {
  local n
  _need_pc
  n="$(_slot_arg "${1:-}")"
  slot_lock "$CLAIM_LOCK" 8
  _begin "$n"
  slot_unlock 8
  _stop "$n"
  slot_drop_databases "$n" || { slot_unlock 7; err "Could not drop slot $n's databases"; }
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
  pid="$(lsof -nP -iTCP:"$(slot_port 0 backend)" -sTCP:LISTEN -t 2>/dev/null | head -1 || true)"
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
  _need_pc
  local row='%-5s %-9s %-24s %-36s %s\n'
  printf "$row" SLOT STATE CONSOLE BRANCH WORKTREE
  slot0="$(_slot0_worktree)"
  if [[ -n "$slot0" ]]; then
    local state0=legacy  # started before process-compose: found by its backend port only
    if slot_pc_running 0; then
      case "$(slot_pc_verdict 0)" in ready) state0=running ;; starting*) state0=starting ;; *) state0=failed ;; esac
    fi
    printf "$row" 0 "$state0" "http://localhost:$(slot_port 0 frontend)" "$(_branch_of "$slot0")" "$slot0"
  fi
  for n in 1 2 3 4 5 6 7 8; do
    [[ -f "$SLOTS_DIR/$n/claim.json" ]] || continue
    printf "$row" "$n" "$(_state "$n")" "http://localhost:$(slot_port "$n" frontend)" \
      "$(_branch_of "$(_claim_field "$n" worktree)")" "$(_claim_field "$n" worktree)"
  done
}

cmd_gc() {
  local n
  # Without process-compose every running slot would read as dead — and be released.
  _need_pc
  # Under the claim lock throughout: a slot found dead is released before anyone can claim it.
  slot_lock "$CLAIM_LOCK" 8
  for n in 1 2 3 4 5 6 7 8; do
    [[ -d "$SLOTS_DIR/$n" ]] || continue
    if [[ ! -f "$SLOTS_DIR/$n/claim.json" ]] || [[ "$(_state "$n")" == dead ]]; then
      # A restart in progress holds the start lock (and its slot reads as dead while it stops).
      slot_lock "$SLOTS_DIR/$n/start.flock" 7 0 2>/dev/null || continue
      _release "$n" || true
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

# For stop-local / reset-local, which take the shared PostgreSQL and Keycloak away: stop every
# slot's stack first (claims and databases kept: 'just up' starts it again), or release them all.
cmd_stop_all() {
  local n busy=""
  for n in 1 2 3 4 5 6 7 8; do
    [[ -f "$SLOTS_DIR/$n/claim.json" ]] || continue
    slot_lock "$SLOTS_DIR/$n/start.flock" 7 0 2>/dev/null || { busy="$busy $n"; continue; }
    # Only a slot that runs is stopped on purpose; a dead one stays dead, for gc to release.
    if slot_pc_running "$n"; then
      _stop "$n"
      touch "$SLOTS_DIR/$n/stopped"
    fi
    slot_unlock 7
    [[ ! -f "$SLOTS_DIR/$n/stopped" ]] || note "Slot $n stopped (claim and databases kept; 'just up' from its worktree starts it again)"
  done
  # A slot mid-start would lose the infrastructure under it: stop here, before compose down.
  [[ -z "$busy" ]] || err "Slot(s)$busy are starting or stopping; try again once they are done"
}
cmd_down_all() {
  local n busy=""
  for n in 1 2 3 4 5 6 7 8; do
    [[ -d "$SLOTS_DIR/$n" ]] || continue
    slot_lock "$SLOTS_DIR/$n/start.flock" 7 0 2>/dev/null || { busy="$busy $n"; continue; }
    # reset-local removes the database volumes next, so a database that cannot be dropped now
    # (PostgreSQL already stopped, e.g. after stop-local) goes with them: release the slot anyway.
    if ! _release "$n" 2>/dev/null; then
      rm -rf "${SLOTS_DIR:?}/$n"
      note "Slot $n released (its databases go with the volumes)"
    fi
  done
  [[ -z "$busy" ]] || err "Slot(s)$busy are starting or stopping; try again once they are done"
}

case "${1:-}" in
  up) shift; cmd_up "$@" ;;
  stop-all) shift; cmd_stop_all ;;
  down-all) shift; cmd_down_all ;;
  sock) shift; cmd_sock "$@" ;;
  restart) shift; cmd_restart "$@" ;;
  down) shift; cmd_down "$@" ;;
  list) shift; cmd_list ;;
  gc) shift; cmd_gc ;;
  db-reset) shift; cmd_db_reset "$@" ;;
  *) sed -n '/^# ─── Local stack slots/,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac

"""Run a stack slot's services without mprocs (ADR-0016).

The mprocs config start-local.sh generates is the one definition of what a stack runs. This
starts each of its `procs` entries as its own process group (cwd, shell, env), records the
group leaders' PIDs in the slot directory, and stops a slot by signalling those groups, which
also reaches uvicorn's reloader children and the `tee` behind each log.

    slot_procs.py start <procs.yaml> <slot_dir> [--skip NAME ...]
    slot_procs.py stop <slot_dir>
    slot_procs.py status <slot_dir> [--all]   # exit 0 if any service is alive (--all: every one)

Each group leader is recorded with its start time, so a PID the system has since given to an
unrelated process (after a reboot, say) is never taken for one of ours, let alone signalled.

Run with: uv run --no-project --with pyyaml python slot_procs.py ...
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import yaml

PIDS_FILE = "pids.json"
STOP_GRACE_SECONDS = 10


def _started(pid: int) -> str | None:
    """The process's start time as `ps` reports it, or None when there is no such process."""
    # check=False: ps exits 1 when the process is gone, which is an answer, not a failure.
    result = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, check=False)
    return result.stdout.strip() or None


def _alive(entry: dict) -> bool:
    started = _started(entry["pgid"])
    if started is None or started != entry["started"]:
        return False
    try:
        os.killpg(entry["pgid"], 0)
    except (ProcessLookupError, PermissionError):
        # PermissionError: the group exists but belongs to someone else, so it is not ours.
        return False
    return True


def _signal(entry: dict, sig: signal.Signals) -> None:
    try:
        os.killpg(entry["pgid"], sig)
    except (ProcessLookupError, PermissionError):
        pass


def _read_pids(slot_dir: Path) -> dict[str, dict]:
    try:
        pids = json.loads((slot_dir / PIDS_FILE).read_text())
    except FileNotFoundError:
        return {}
    # A slot started before start times were recorded has bare pgids: take them as they are.
    return {
        name: entry if isinstance(entry, dict) else {"pgid": entry, "started": _started(entry)}
        for name, entry in pids.items()
    }


def start(procs_file: Path, slot_dir: Path, skip: set[str]) -> None:
    procs = yaml.safe_load(procs_file.read_text())["procs"]
    pids = _read_pids(slot_dir)
    for name, proc in procs.items():
        if name in skip:
            continue
        if name in pids and _alive(pids[name]):
            print(f"  {name}: already running (pgid {pids[name]['pgid']})")
            continue
        env = {**os.environ, **{key: str(value) for key, value in (proc.get("env") or {}).items()}}
        child = subprocess.Popen(
            ["bash", "-c", proc["shell"]],
            cwd=proc.get("cwd"),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        pids[name] = {"pgid": child.pid, "started": _started(child.pid)}
        print(f"  {name}: started (pgid {child.pid})")
    (slot_dir / PIDS_FILE).write_text(json.dumps(pids, indent=2))


def stop(slot_dir: Path) -> None:
    pids = _read_pids(slot_dir)
    for entry in pids.values():
        if _alive(entry):
            _signal(entry, signal.SIGTERM)
    deadline = time.monotonic() + STOP_GRACE_SECONDS
    while time.monotonic() < deadline and any(_alive(entry) for entry in pids.values()):
        time.sleep(0.5)
    for name, entry in pids.items():
        if _alive(entry):
            print(f"  {name}: did not stop on SIGTERM, killing")
            _signal(entry, signal.SIGKILL)
    (slot_dir / PIDS_FILE).unlink(missing_ok=True)


def status(slot_dir: Path, require_all: bool) -> bool:
    pids = _read_pids(slot_dir)
    alive = {name: _alive(entry) for name, entry in pids.items()}
    for name, entry in pids.items():
        print(f"{name}: {'up' if alive[name] else 'down'} (pgid {entry['pgid']})")
    if not alive:
        return False
    return all(alive.values()) if require_all else any(alive.values())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    start_cmd = sub.add_parser("start")
    start_cmd.add_argument("procs_file", type=Path)
    start_cmd.add_argument("slot_dir", type=Path)
    start_cmd.add_argument("--skip", action="append", default=[])
    sub.add_parser("stop").add_argument("slot_dir", type=Path)
    status_cmd = sub.add_parser("status")
    status_cmd.add_argument("slot_dir", type=Path)
    status_cmd.add_argument("--all", action="store_true", dest="require_all")
    args = parser.parse_args()

    if args.command == "start":
        start(args.procs_file, args.slot_dir, set(args.skip))
    elif args.command == "stop":
        stop(args.slot_dir)
    else:
        sys.exit(0 if status(args.slot_dir, args.require_all) else 1)


if __name__ == "__main__":
    main()

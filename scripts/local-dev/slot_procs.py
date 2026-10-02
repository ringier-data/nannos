"""Run a stack slot's services without mprocs (ADR-0016).

The mprocs config start-local.sh generates is the one definition of what a stack runs. This
starts each of its `procs` entries as its own process group (cwd, shell, env), records the
group leaders' PIDs in the slot directory, and stops a slot by signalling those groups, which
also reaches uvicorn's reloader children and the `tee` behind each log.

    slot_procs.py start <procs.yaml> <slot_dir> [--skip NAME ...]
    slot_procs.py stop <slot_dir>
    slot_procs.py status <slot_dir>     # exit 0 if any service is alive

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


def _alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_pids(slot_dir: Path) -> dict[str, int]:
    try:
        return json.loads((slot_dir / PIDS_FILE).read_text())
    except FileNotFoundError:
        return {}


def start(procs_file: Path, slot_dir: Path, skip: set[str]) -> None:
    procs = yaml.safe_load(procs_file.read_text())["procs"]
    pids = _read_pids(slot_dir)
    for name, proc in procs.items():
        if name in skip:
            continue
        if name in pids and _alive(pids[name]):
            print(f"  {name}: already running (pgid {pids[name]})")
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
        pids[name] = child.pid
        print(f"  {name}: started (pgid {child.pid})")
    (slot_dir / PIDS_FILE).write_text(json.dumps(pids, indent=2))


def stop(slot_dir: Path) -> None:
    pids = _read_pids(slot_dir)
    for pgid in pids.values():
        if _alive(pgid):
            os.killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + STOP_GRACE_SECONDS
    while time.monotonic() < deadline and any(_alive(pgid) for pgid in pids.values()):
        time.sleep(0.5)
    for name, pgid in pids.items():
        if _alive(pgid):
            print(f"  {name}: did not stop on SIGTERM, killing")
            os.killpg(pgid, signal.SIGKILL)
    (slot_dir / PIDS_FILE).unlink(missing_ok=True)


def status(slot_dir: Path) -> bool:
    pids = _read_pids(slot_dir)
    for name, pgid in pids.items():
        print(f"  {name}: {'up' if _alive(pgid) else 'down'} (pgid {pgid})")
    return any(_alive(pgid) for pgid in pids.values())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    start_cmd = sub.add_parser("start")
    start_cmd.add_argument("procs_file", type=Path)
    start_cmd.add_argument("slot_dir", type=Path)
    start_cmd.add_argument("--skip", action="append", default=[])
    for name in ("stop", "status"):
        sub.add_parser(name).add_argument("slot_dir", type=Path)
    args = parser.parse_args()

    if args.command == "start":
        start(args.procs_file, args.slot_dir, set(args.skip))
    elif args.command == "stop":
        stop(args.slot_dir)
    else:
        sys.exit(0 if status(args.slot_dir) else 1)


if __name__ == "__main__":
    main()

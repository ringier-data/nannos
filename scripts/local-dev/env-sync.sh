#!/usr/bin/env bash
set -euo pipefail

# Copy the gitignored env files listed in worktree-env-files from the main checkout into this
# worktree. A file the worktree already has is kept, so a per-branch tweak stays local;
# --force refreshes every listed file from the main checkout (e.g. after a secret rotated).
# Copies, not symlinks: a symlink would write a worktree's edit back into the main checkout.
#
#   env-sync.sh [--force] [--quiet]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
MAIN_ROOT="$(cd "$(git -C "$ROOT_DIR" rev-parse --path-format=absolute --git-common-dir)/.." && pwd)"

force=""
quiet=""
for arg in "$@"; do
  case $arg in
    --force) force=1 ;;
    --quiet) quiet=1 ;;
    *) echo "Unknown flag: $arg" >&2; exit 1 ;;
  esac
done

say() { [[ -n "$quiet" ]] || printf '%s\n' "$*"; }

if [[ "$ROOT_DIR" == "$MAIN_ROOT" ]]; then
  say "This is the main checkout; nothing to sync."
  exit 0
fi

while IFS= read -r path || [[ -n "$path" ]]; do
  path="${path%%#*}"
  path="${path%"${path##*[![:space:]]}"}"
  [[ -n "$path" ]] || continue
  if [[ ! -f "$MAIN_ROOT/$path" ]]; then
    say "  - $path (not in the main checkout)"
  elif [[ -f "$ROOT_DIR/$path" && -z "$force" ]]; then
    say "  = $path (kept)"
  else
    mkdir -p "$(dirname "$ROOT_DIR/$path")"
    cp -p "$MAIN_ROOT/$path" "$ROOT_DIR/$path"
    printf '  + %s (from %s)\n' "$path" "$MAIN_ROOT" >&2
  fi
done < "$SCRIPT_DIR/worktree-env-files"

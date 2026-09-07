#!/usr/bin/env bash
#
# Helpers for "hosts": apps OUTSIDE this monorepo that consume @nannos/embed-sdk
# from the npm registry (e.g. the cockpit frontend). Sourced by justfile recipes
# after scripts/release-helpers.sh.
#
# A host is registered with a gitignored symlink — same pattern as `gitops`:
#   ln -s /path/to/rcplus-alloy-cockpit-frontend/app hosts/cockpit
# The link points at the directory holding the package.json that depends on
# @nannos/embed-sdk.
#
# Two states matter for a host, and the recipes move between them:
#   linked    node_modules/@nannos/embed-sdk is a symlink to packages/embed-sdk
#             (local development; package.json and lockfile are untouched, so
#             nothing can leak into a commit — `npm install` restores the registry copy)
#   registry  node_modules/@nannos/embed-sdk is the published tarball the lockfile pins
#
# Compatible with bash 3.2+ (macOS default).
#

SDK_PKG="embed-sdk"
SDK_NAME="@nannos/embed-sdk"
HOSTS_DIR="hosts"

# ── Registry (of hosts) ─────────────────────────────────────────────

host_names() {
  [[ -d "$HOSTS_DIR" ]] || return 0
  local entry
  for entry in "$HOSTS_DIR"/*; do
    [[ -e "$entry" || -L "$entry" ]] || continue
    basename "$entry"
  done
}

# Resolve a host name to its app directory. Errors (with the fix) when unknown.
host_dir() {
  local name="$1" link="${HOSTS_DIR}/$1" dir
  if [[ ! -L "$link" && ! -d "$link" ]]; then
    echo "❌ Unknown host '${name}'." >&2
    local known; known="$(host_names | tr '\n' ' ')"
    if [[ -n "$known" ]]; then
      echo "   Registered hosts: ${known}" >&2
    else
      echo "   No hosts registered yet." >&2
    fi
    echo "   Register one with a symlink to the app dir (the one with the package.json):" >&2
    echo "     mkdir -p ${HOSTS_DIR} && ln -s /path/to/rcplus-alloy-cockpit-frontend/app ${HOSTS_DIR}/cockpit" >&2
    return 1
  fi
  dir="$(cd "$link" 2>/dev/null && pwd -P)" || {
    echo "❌ Host '${name}' points at a directory that does not exist: $(readlink "$link")" >&2
    return 1
  }
  if [[ ! -f "${dir}/package.json" ]]; then
    echo "❌ Host '${name}' (${dir}) has no package.json" >&2
    return 1
  fi
  echo "$dir"
}

# ── Inspecting one host ─────────────────────────────────────────────

# The version range the host's package.json declares for the SDK ("" if none).
host_range() {
  local dir="$1"
  node -e '
    const j = require(process.argv[1] + "/package.json");
    for (const k of ["dependencies", "devDependencies", "peerDependencies"]) {
      if (j[k] && j[k][process.argv[2]] !== undefined) { process.stdout.write(j[k][process.argv[2]]); process.exit(0); }
    }
  ' "$dir" "$SDK_NAME"
}

# Which package.json section declares the SDK: "dependencies" | "devDependencies" | ""
host_dep_section() {
  local dir="$1"
  node -e '
    const j = require(process.argv[1] + "/package.json");
    for (const k of ["dependencies", "devDependencies"]) {
      if (j[k] && j[k][process.argv[2]] !== undefined) { process.stdout.write(k); process.exit(0); }
    }
  ' "$dir" "$SDK_NAME"
}

# What is physically in node_modules: "linked <target>" | "registry <version>" | "missing"
host_installed() {
  local dir="$1" mod="${1}/node_modules/${SDK_NAME}"
  if [[ -L "$mod" ]]; then
    echo "linked $(cd "$mod" 2>/dev/null && pwd -P || readlink "$mod")"
  elif [[ -f "${mod}/package.json" ]]; then
    echo "registry $(node -p "require('${mod}/package.json').version")"
  else
    echo "missing"
  fi
}

# What the lockfile pins: "registry <version>" | "file <path>" | "absent" | "no-lockfile"
host_locked() {
  local dir="$1"
  [[ -f "${dir}/package-lock.json" ]] || { echo "no-lockfile"; return 0; }
  node -e '
    const lock = require(process.argv[1] + "/package-lock.json");
    const e = (lock.packages || {})["node_modules/" + process.argv[2]];
    if (!e) { process.stdout.write("absent"); process.exit(0); }
    if (e.link) { process.stdout.write("file " + e.resolved); process.exit(0); }
    process.stdout.write("registry " + e.version);
  ' "$dir" "$SDK_NAME"
}

# Does a version satisfy the package.json range? Echoes true/false ("unknown" if
# no semver module can be found). Usage: host_range_satisfied <host-dir> <range> <version>
host_range_satisfied() {
  local dir="$1" range="$2" version="$3"
  node -e '
    const [dir, range, version] = process.argv.slice(1);
    // No dependency of our own: use the host tree'"'"'s semver, else the copy bundled in npm.
    let semver;
    try { semver = require(require.resolve("semver", { paths: [dir] })); }
    catch { semver = require(require("path").join(require("child_process").execSync("npm root -g").toString().trim(), "npm/node_modules/semver")); }
    process.stdout.write(String(semver.satisfies(version, range)));
  ' "$dir" "$range" "$version" 2>/dev/null || echo "unknown"
}

# ── Inspecting the SDK checkout ─────────────────────────────────────

# One line: "v0.3.0 — 4 unreleased commits since embed-sdk/v0.2.0 (working tree dirty)"
sdk_checkout_summary() {
  local version tag dir count dirty=""
  version="$(get_package_version "$SDK_PKG")"
  tag="$(get_last_tag "$SDK_PKG")"
  dir="$(pkg_dir "$SDK_PKG")"
  if ! git diff --quiet -- "$dir" || ! git diff --cached --quiet -- "$dir"; then
    dirty=" (working tree dirty)"
  fi
  if [[ -z "$tag" ]]; then
    echo "v${version} — never released${dirty}"
    return 0
  fi
  count="$(git rev-list --count "${tag}..HEAD" -- "$dir")"
  if [[ "$count" == "0" ]]; then
    echo "v${version} — no unreleased commits since ${tag}${dirty}"
  else
    echo "v${version} — ${count} unreleased commit(s) since ${tag}${dirty}"
  fi
}

# ── Mutating a host ─────────────────────────────────────────────────

# Block until a version is visible on the registry (publishes propagate with a
# short delay). Usage: wait_for_npm_version <version> [timeout-seconds]
wait_for_npm_version() {
  local version="$1" timeout="${2:-90}" waited=0
  while ! npm_version_published "$SDK_NAME" "$version"; do
    if (( waited >= timeout )); then
      echo "❌ ${SDK_NAME}@${version} is not on ${NPM_REGISTRY} (waited ${timeout}s)." >&2
      echo "   Release it first (just release / just release-pkg embed-sdk), or pass a published version." >&2
      return 1
    fi
    if (( waited == 0 )); then
      printf "   waiting for %s@%s to appear on the registry" "$SDK_NAME" "$version" >&2
    fi
    printf "." >&2
    sleep 3
    waited=$((waited + 3))
  done
  (( waited > 0 )) && echo "" >&2
  return 0
}

# Run `just host-bump` for every registered host. Never fatal: by the time this
# runs the release is published and pushed, so a host problem must not fail the
# release — it is reported with the command to retry.
bump_all_hosts() {
  local name ok=0 failed=""
  for name in $(host_names); do
    if just host-bump "$name"; then
      ok=$((ok + 1))
    else
      failed="${failed} ${name}"
    fi
  done
  if [[ -n "$failed" ]]; then
    printf '\033[1;33m⚠️  Host bump failed for:%s — retry with: just host-bump <name>\033[0m\n' "$failed"
  fi
}

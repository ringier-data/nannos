#!/usr/bin/env bash
#
# Release helper functions for per-package versioning.
# Sourced by justfile recipes.
#
# Each package has its own semver version stored in:
#   - package.json  (Node.js packages)
#   - pyproject.toml (Python packages)
#   - VERSION file   (Docker-only packages)
#
# Git tags follow the format: <package>/v<version>
# e.g. app1/v1.14.0, app2/v1.13.1
#
# Compatible with bash 3.2+ (macOS default).
#

ALL_PACKAGES="agent-creator agent-runner orchestrator-agent console-backend console-frontend ringier-a2a-sdk client-slack client-slack-frontend"

# ── Package metadata (case-based, bash 3.2 compatible) ──────────────

pkg_dir() {
  case "$1" in
    agent-creator)           echo "packages/agent-creator" ;;
    agent-runner)            echo "packages/agent-runner" ;;
    orchestrator-agent)      echo "packages/orchestrator-agent" ;;
    console-backend)         echo "packages/console-backend" ;;
    console-frontend)        echo "packages/console-frontend" ;;
    ringier-a2a-sdk)         echo "packages/ringier-a2a-sdk" ;;
    client-slack)            echo "packages/client-slack" ;;
    client-slack-frontend)   echo "packages/client-slack-frontend" ;;
    *) echo "ERROR: Unknown package '$1'" >&2; return 1 ;;
  esac
}

pkg_type() {
  case "$1" in
    console-frontend|client-slack-frontend|client-slack) echo "node" ;;
    agent-creator|agent-runner|orchestrator-agent|console-backend|ringier-a2a-sdk) echo "python" ;;
    *) echo "ERROR: Unknown package '$1'" >&2; return 1 ;;
  esac
}

# ── Helpers ─────────────────────────────────────────────────────────

# Read version from the appropriate file for a package
get_package_version() {
  local pkg="$1"
  local dir type
  dir="$(pkg_dir "$pkg")"
  type="$(pkg_type "$pkg")"

  case "$type" in
    node)
      node -p "require('./${dir}/package.json').version"
      ;;
    python)
      sed -n 's/^version = "\(.*\)"/\1/p' "${dir}/pyproject.toml"
      ;;
    version)
      tr -d '[:space:]' < "${dir}/VERSION"
      ;;
  esac
}

# Find the latest git tag for a package (e.g. "app1/v1.13.0")
get_last_tag() {
  local pkg="$1"
  git tag -l "${pkg}/v*" --sort=-v:refname | head -1
}

# Check if a package has changes since its last release tag
has_changes() {
  local pkg="$1"
  local tag
  tag="$(get_last_tag "$pkg")"

  if [[ -z "$tag" ]]; then
    # No tag exists → treat as changed
    echo "true"
    return 0
  fi

  local dirs
  dirs="$(pkg_dir "$pkg")"

  if git diff --quiet "${tag}..HEAD" -- $dirs 2>/dev/null; then
    echo "false"
  else
    echo "true"
  fi
}

# Bump version in the appropriate file(s) for a package
bump_version() {
  local pkg="$1"
  local part="$2"  # major, minor, or patch
  local dir type current_version
  dir="$(pkg_dir "$pkg")"
  type="$(pkg_type "$pkg")"
  current_version="$(get_package_version "$pkg")"

  # Parse semver
  local major minor patch
  IFS='.' read -r major minor patch <<< "$current_version"

  case "$part" in
    major) major=$((major + 1)); minor=0; patch=0 ;;
    minor) minor=$((minor + 1)); patch=0 ;;
    patch) patch=$((patch + 1)) ;;
    *)
      echo "ERROR: Invalid bump part '${part}'. Use major, minor, or patch." >&2
      return 1
      ;;
  esac

  local new_version="${major}.${minor}.${patch}"

  case "$type" in
    node)
      # Update package.json (and package-lock.json if it exists)
      cd "$dir"
      npm version "$new_version" --no-git-tag-version --allow-same-version > /dev/null
      cd - > /dev/null
      ;;
    python)
      sed -i '' "s/^version = \"${current_version}\"/version = \"${new_version}\"/" "${dir}/pyproject.toml"
      ;;
    version)
      echo "$new_version" > "${dir}/VERSION"
      ;;
  esac

  echo "$new_version"
}

# Determine bump action (major/minor/patch) from conventional commits since last tag
# Scoped to the package's directory.
get_bump_action() {
  local pkg="$1"
  local tag dir log_range
  tag="$(get_last_tag "$pkg")"
  dir="$(pkg_dir "$pkg")"

  if [[ -n "$tag" ]]; then
    log_range="${tag}..HEAD"
  else
    log_range="HEAD"
  fi

  local commits
  commits="$(git log --pretty=format:'%B' "$log_range" -- "$dir")"

  # BREAKING CHANGE: type(scope)!: or footer BREAKING CHANGE:
  local regex_pattern='^[a-z]+(\([^ ]*\))?!:'
  if echo "$commits" | grep -Eic "$regex_pattern" > /dev/null 2>&1 && [[ $(echo "$commits" | grep -Eic "$regex_pattern") -gt 0 ]]; then
    echo "major"
  elif [[ $(echo "$commits" | grep -c "BREAKING CHANGE:") -gt 0 ]]; then
    echo "major"
  elif echo "$commits" | grep -iq "^feat"; then
    echo "minor"
  else
    echo "patch"
  fi
}

# Preview bumped version without writing to disk
preview_bump() {
  local pkg="$1"
  local part="$2"
  local current_version
  current_version="$(get_package_version "$pkg")"

  local major minor patch
  IFS='.' read -r major minor patch <<< "$current_version"

  case "$part" in
    major) major=$((major + 1)); minor=0; patch=0 ;;
    minor) minor=$((minor + 1)); patch=0 ;;
    patch) patch=$((patch + 1)) ;;
  esac

  echo "${major}.${minor}.${patch}"
}

# Create a git tag for a package release
tag_package() {
  local pkg="$1"
  local version="$2"
  git tag "${pkg}/v${version}" -m "release: ${pkg}/v${version}"
}

# ── Tmux build-pane helpers ─────────────────────────────────────────
# Show a live tail of build logs in a tmux split pane.
# On success the pane is killed; on failure it stays open.

# Print a one-time hint when running outside tmux.
tmux_tip() {
  if [[ -z "${TMUX:-}" ]]; then
    printf '\033[2m💡 Tip: run inside tmux for a live build-log pane\033[0m\n'
  fi
}

# Open a tmux pane tailing a logfile. Prints the pane ID to stdout.
# Usage: PANE=$(build_pane_start "label" "/path/to/logfile")
build_pane_start() {
  local label="$1" logfile="$2"
  if [[ -z "${TMUX:-}" ]]; then return; fi
  tmux split-window -v -l 15 -d -P -F '#{pane_id}' \
    "printf '\\033[1;36m── %s ──\\033[0m\\n' '${label}'; tail -f '${logfile}'"
}

# Close the tmux pane when the build finishes.
# Usage: build_pane_end "$PANE"
build_pane_end() {
  local pane_id="$1"
  if [[ -z "${TMUX:-}" || -z "$pane_id" ]]; then return; fi
  tmux kill-pane -t "$pane_id" 2>/dev/null || true
}

# Run a command with a tmux log pane (when inside tmux) or tee to stdout (outside tmux).
# Usage: build_with_pane "label" logfile docker buildx build ...
build_with_pane() {
  local label="$1" logfile="$2"
  shift 2
  local rc=0
  if [[ -n "${TMUX:-}" ]]; then
    # Inside tmux: redirect to logfile, show tail in a split pane
    local pane
    pane=$(build_pane_start "$label" "$logfile")
    "$@" >> "$logfile" 2>&1 || rc=$?
    build_pane_end "$pane"
  else
    # Outside tmux: tee output so it's visible in the terminal
    "$@"
  fi
  return $rc
}

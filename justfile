# List all available recipes (default when running `just` with no args)
default:
    @just --list

# ─── Per-Package Versioning & Release ──────────────────────────────
#
# Each package has its own semver version stored in:
#   - package.json   (Node.js packages: console-frontend)
#   - pyproject.toml (Python packages: everything else)
#
# Git tags: <package>/v<version>  (e.g. orchestrator-agent/v0.7.0)
#
# Workflow:
#   just changed                            → see which packages changed since last release
#   just release                             → auto-bump & tag all changed packages
#   just release patch                       → force bump level for all changed packages
#   just release-pkg orchestrator-agent       → auto-bump & tag a single package
#   just release-pkg orchestrator-agent patch → force bump level for a single package
#   just publish-npm embed-sdk               → publish a package's current version to npm
#   just hosts                              → how external apps (cockpit, …) consume @nannos/embed-sdk
#   just host-link cockpit                  → develop a host against this SDK checkout
#   just host-bump cockpit                  → move a host onto the published SDK (also done by `just release`)
#   just build                              → build Docker images for all buildable packages
#   just push=true build                    → build & push Docker images
#   just build-pkg orchestrator-agent       → build a single package image
#   just push=true build-pkg orchestrator-agent → build & push a single package image

# ─── Configuration ─────────────────────────────────────────────────

# TODO: set your container registry
registry := "ghcr.io/ringier-data"

# Per-package image names (only packages with Dockerfiles)
img_agent_runner     := registry + "/nannos-agent-runner"
img_orchestrator     := registry + "/nannos-orchestrator-agent"
img_console_backend  := registry + "/nannos-console-backend"
img_console_frontend := registry + "/nannos-console-frontend"
img_client_slack := registry + "/nannos-client-slack"
img_client_slack_frontend := registry + "/nannos-client-slack-frontend"
img_client_email := registry + "/nannos-client-email"
img_voice_agent := registry + "/nannos-voice-agent"
img_catalog_worker := registry + "/nannos-catalog-worker"
img_client_google_chat := registry + "/nannos-client-google-chat"
img_soffice_worker := registry + "/nannos-soffice-worker"
img_litellm_proxy := registry + "/nannos-litellm-proxy"

# Default build platform
platform := "linux/arm64"

# Timestamp for dev prerelease suffix (YYYYMMDDHHmmss UTC)
build_ts := `date -u +%Y%m%d%H%M%S`

# Packages that have Dockerfiles (used by build recipes)
_buildable_packages := "agent-runner orchestrator-agent console-backend catalog-worker console-frontend client-slack client-slack-frontend client-email voice-agent client-google-chat soffice-worker litellm-proxy"

# Build flags (override on CLI, e.g. just push=true build)
push := ""
tag := ""
all_archs := ""

# ─── Version Helpers ───────────────────────────────────────────────

# Show a package's current version
[private]
pkg-version pkg:
    #!/usr/bin/env bash
    set -euo pipefail
    source scripts/release-helpers.sh
    get_package_version "{{ pkg }}"

# Map package name → image ref
[private]
pkg-image pkg:
    #!/usr/bin/env bash
    case "{{ pkg }}" in
      agent-runner)       echo "{{ img_agent_runner }}" ;;
      orchestrator-agent) echo "{{ img_orchestrator }}" ;;
      console-backend)    echo "{{ img_console_backend }}" ;;
      console-frontend)   echo "{{ img_console_frontend }}" ;;
      client-slack)       echo "{{ img_client_slack }}" ;;
      client-slack-frontend) echo "{{ img_client_slack_frontend }}" ;;
      client-email)       echo "{{ img_client_email }}" ;;
      voice-agent)        echo "{{ img_voice_agent }}" ;;
      catalog-worker)     echo "{{ img_catalog_worker }}" ;;
      client-google-chat) echo "{{ img_client_google_chat }}" ;;
      soffice-worker)     echo "{{ img_soffice_worker }}" ;;
      litellm-proxy)      echo "{{ img_litellm_proxy }}" ;;
      *) echo "" ;;
    esac

# ─── Release ───────────────────────────────────────────────────────

# Show which packages have changed since their last release
changed:
    #!/usr/bin/env bash
    set -euo pipefail
    source scripts/release-helpers.sh
    CYAN='\033[1;36m' GREEN='\033[1;32m' DIM='\033[2m' YELLOW='\033[1;33m' RESET='\033[0m'
    for pkg in $ALL_PACKAGES; do
      VERSION=$(get_package_version "$pkg")
      LAST_TAG=$(get_last_tag "$pkg")
      CHANGED=$(has_changes "$pkg")
      if [[ "$CHANGED" == "true" ]]; then
        BUMP=$(get_bump_action "$pkg")
        NEXT=$(preview_bump "$pkg" "$BUMP")
        printf "${YELLOW}● %-30s${RESET} v%-10s → v%-10s ${DIM}(%s, last tag: %s)${RESET}\n" "$pkg" "$VERSION" "$NEXT" "$BUMP" "${LAST_TAG:-none}"
      else
        printf "${DIM}  %-30s v%-10s (%s)${RESET}\n" "$pkg" "$VERSION" "${LAST_TAG:-none}"
      fi
    done

# Detect changed packages, bump versions, commit, tag, docker(build&push)
release bump="":
    #!/usr/bin/env bash
    # -E: the rollback ERR trap must also fire for failures inside helper
    # functions (publish_npm_package), not only for top-level commands.
    set -Eeuo pipefail
    source scripts/release-helpers.sh
    source scripts/host-helpers.sh

    CYAN='\033[1;36m' GREEN='\033[1;32m' RED='\033[1;31m' DIM='\033[2m' YELLOW='\033[1;33m' RESET='\033[0m'

    # Detect which packages have changes since their last release tag
    CHANGED=()
    UNCHANGED=()
    for pkg in $ALL_PACKAGES; do
      if [[ "$(has_changes "$pkg")" == "true" ]]; then
        CHANGED+=("$pkg")
      else
        UNCHANGED+=("$pkg")
      fi
    done

    if [[ ${#CHANGED[@]} -eq 0 ]]; then
      echo "✅ No packages have changes since their last release."
      exit 0
    fi

    just changed
    echo ""

    # `npm publish` is the one release step that cannot be rolled back, so its
    # credentials are checked while the working tree is still untouched.
    SDK_RELEASED=false
    for pkg in "${CHANGED[@]}"; do
      if is_npm_package "$pkg"; then
        require_npm_auth
      fi
      [[ "$pkg" == "$SDK_PKG" ]] && SDK_RELEASED=true
    done
    # Releasing the SDK moves its hosts (Phase 5) — show where they stand first.
    if [[ "$SDK_RELEASED" == "true" && -n "$(host_names)" ]]; then
      just hosts
      echo ""
    fi

    # Phase 1: Bump versions, commit & tag
    RELEASES=()
    TAGS=()
    BUILDABLE="{{ _buildable_packages }}"
    for pkg in "${CHANGED[@]}"; do
      BUMP="{{ bump }}"
      if [[ -z "$BUMP" ]]; then
        BUMP=$(get_bump_action "$pkg")
      fi
      printf "${CYAN}🔄 Bumping %s (%s)...${RESET}\n" "$pkg" "$BUMP"
      NEW_VERSION=$(bump_version "$pkg" "$BUMP")
      printf "   v%s\n" "$NEW_VERSION"
      RELEASES+=("${pkg}/v${NEW_VERSION}")
      TAGS+=("${pkg}/v${NEW_VERSION}")
    done
    echo ""

    # Refresh non-released shared libs' lockfiles so their editable path-dep
    # versions stay in sync with the packages we just bumped, and the root
    # npm-workspace lockfile so its member versions match (console-frontend's
    # image build runs `npm ci`, which rejects a mismatch).
    refresh_shared_lockfiles
    refresh_node_lockfile

    RELEASE_MSG="release: $(IFS=', '; echo "${RELEASES[*]}")"
    git add -A
    COMMIT_SHA=$(git commit -m "$RELEASE_MSG" --quiet && git rev-parse HEAD)
    for tag_name in "${TAGS[@]}"; do
      git tag "$tag_name" -m "release: $tag_name"
    done
    printf "${GREEN}✅ Released: %s${RESET}\n\n" "${RELEASES[*]}"

    # Rollback helper: undo commit and tags on failure
    rollback() {
      printf "\n${RED}💥 Build/push failed — rolling back release commit and tags...${RESET}\n"
      for tag_name in "${TAGS[@]}"; do
        git tag -d "$tag_name" 2>/dev/null || true
      done
      git reset --soft HEAD~1
      git restore --staged .
      git checkout -- .
      printf "${YELLOW}↩️  Rolled back to previous state. Git history is clean.${RESET}\n"
      exit 1
    }
    trap rollback ERR

    # Phase 2: Build all (uses bumped versions, warms cache)
    for pkg in "${CHANGED[@]}"; do
      if [[ " $BUILDABLE " =~ " $pkg " ]]; then
        just build-pkg "$pkg"
      fi
    done
    # Also build virtual packages that share a parent's directory
    for vpkg in $VIRTUAL_PACKAGES; do
      parent_dir="$(pkg_dir "$vpkg")"
      for pkg in "${CHANGED[@]}"; do
        if [[ "$(pkg_dir "$pkg")" == "$parent_dir" ]]; then
          just build-pkg "$vpkg"
          break
        fi
      done
    done

    # Phase 3: Push all (reuses cached builds), then publish to npm
    for pkg in "${CHANGED[@]}"; do
      if [[ " $BUILDABLE " =~ " $pkg " ]]; then
        just push=true build-pkg "$pkg"
      fi
    done
    for vpkg in $VIRTUAL_PACKAGES; do
      parent_dir="$(pkg_dir "$vpkg")"
      for pkg in "${CHANGED[@]}"; do
        if [[ "$(pkg_dir "$pkg")" == "$parent_dir" ]]; then
          just push=true build-pkg "$vpkg"
          break
        fi
      done
    done

    for pkg in "${CHANGED[@]}"; do
      if is_npm_package "$pkg"; then
        printf "${CYAN}📦 Publishing %s to npm...${RESET}\n" "$pkg"
        publish_npm_package "$pkg"
        printf "${GREEN}✅ Published %s@%s${RESET}\n" "$(npm_package_name "$pkg")" "$(get_package_version "$pkg")"
      fi
    done

    # Phase 4: Push git commit and tags to remote
    trap - ERR  # Clear rollback — images are pushed and npm versions are permanent
    printf "${CYAN}🚀 Pushing release commit and tags...${RESET}"
    git push && git push --tags
    printf "${GREEN} ✓${RESET}\n"

    # Phase 5: Move registered hosts onto the SDK version just published. The
    # release is already out, so a host problem is reported with a retry command
    # rather than failing the run.
    if [[ "$SDK_RELEASED" == "true" ]]; then
      echo ""
      if [[ -n "$(host_names)" ]]; then
        bump_all_hosts
      else
        printf "${DIM}No hosts registered — external consumers of %s bump manually (see: just hosts).${RESET}\n" "$SDK_NAME"
      fi
    fi

# Release a single package (bump version, commit, tag, build, push)
release-pkg pkg bump="":
    #!/usr/bin/env bash
    # -E: the rollback ERR trap must also fire for failures inside helper
    # functions (publish_npm_package), not only for top-level commands.
    set -Eeuo pipefail
    source scripts/release-helpers.sh
    source scripts/host-helpers.sh

    CYAN='\033[1;36m' GREEN='\033[1;32m' RED='\033[1;31m' DIM='\033[2m' YELLOW='\033[1;33m' RESET='\033[0m'
    PKG="{{ pkg }}"
    BUMP="{{ bump }}"

    # Validate package name
    if [[ ! " $ALL_PACKAGES " =~ " $PKG " ]]; then
      echo "❌ Unknown package: $PKG"
      echo "   Available: $ALL_PACKAGES"
      exit 1
    fi

    # `npm publish` is the one release step that cannot be rolled back, so its
    # credentials are checked while the working tree is still untouched.
    if is_npm_package "$PKG"; then
      require_npm_auth
    fi
    # Releasing the SDK moves its hosts (Phase 5) — show where they stand first.
    if [[ "$PKG" == "$SDK_PKG" && -n "$(host_names)" ]]; then
      just hosts
      echo ""
    fi

    if [[ -z "$BUMP" ]]; then
      BUMP=$(get_bump_action "$PKG")
    fi

    CURRENT=$(get_package_version "$PKG")
    IMAGE=$(just pkg-image "$PKG")

    # Phase 1: Bump, commit & tag
    printf "${CYAN}🔄 Bumping %s from v%s (%s)...${RESET}\n" "$PKG" "$CURRENT" "$BUMP"
    NEW_VERSION=$(bump_version "$PKG" "$BUMP")
    TAG_NAME="${PKG}/v${NEW_VERSION}"
    printf "   → v%s\n\n" "$NEW_VERSION"

    # Refresh non-released shared libs' lockfiles so their editable path-dep
    # versions stay in sync with the package we just bumped, and the root
    # npm-workspace lockfile so its member versions match (console-frontend's
    # image build runs `npm ci`, which rejects a mismatch).
    refresh_shared_lockfiles
    refresh_node_lockfile

    git add -A
    git commit -m "release: $TAG_NAME" --quiet
    git tag "$TAG_NAME" -m "release: $TAG_NAME"
    printf "${GREEN}✅ Released ${TAG_NAME}${RESET}\n"

    # Rollback helper: undo commit and tag on failure
    rollback() {
      printf "\n${RED}💥 Build/push failed — rolling back release commit and tag...${RESET}\n"
      git tag -d "$TAG_NAME" 2>/dev/null || true
      git reset --soft HEAD~1
      git restore --staged .
      git checkout -- .
      printf "${YELLOW}↩️  Rolled back to previous state. Git history is clean.${RESET}\n"
      exit 1
    }
    trap rollback ERR

    # Phase 2: Build (warms cache)
    if [[ -n "$IMAGE" ]]; then
      just build-pkg "$PKG"
    fi

    # Phase 3: Push (reuses cached build), then publish to npm
    if [[ -n "$IMAGE" ]]; then
      just push=true build-pkg "$PKG"
    fi
    if is_npm_package "$PKG"; then
      printf "${CYAN}📦 Publishing %s to npm...${RESET}\n" "$PKG"
      publish_npm_package "$PKG"
      printf "${GREEN}✅ Published %s@%s${RESET}\n" "$(npm_package_name "$PKG")" "$NEW_VERSION"
    fi

    # Phase 4: Push git commit and tag to remote
    trap - ERR  # Clear rollback — the image is pushed and npm versions are permanent
    printf "${CYAN}🚀 Pushing release commit and tag...${RESET}"
    git push && git push --tags
    printf "${GREEN} ✓${RESET}\n"

    # Phase 5: Move registered hosts onto the SDK version just published (the
    # release is out — a host problem is reported with a retry command, not fatal).
    if [[ "$PKG" == "$SDK_PKG" ]]; then
      echo ""
      if [[ -n "$(host_names)" ]]; then
        bump_all_hosts
      else
        printf "${DIM}No hosts registered — external consumers of %s bump manually (see: just hosts).${RESET}\n" "$SDK_NAME"
      fi
    fi

# Normally part of `just release`; use this to retry a failed publish or to
# dry-run one:  just publish-npm embed-sdk --dry-run
#
# Publish an npm package at its CURRENT version (no bump, no tag)
publish-npm pkg *args:
    #!/usr/bin/env bash
    set -euo pipefail
    source scripts/release-helpers.sh

    CYAN='\033[1;36m' GREEN='\033[1;32m' RESET='\033[0m'
    PKG="{{ pkg }}"

    if ! is_npm_package "$PKG"; then
      echo "❌ '$PKG' is not published to npm."
      echo "   npm packages: $NPM_PACKAGES"
      exit 1
    fi

    require_npm_auth
    printf "${CYAN}📦 Publishing %s@%s to npm...${RESET}\n" "$(npm_package_name "$PKG")" "$(get_package_version "$PKG")"
    publish_npm_package "$PKG" {{ args }}
    printf "${GREEN}✅ Done${RESET}\n"

# ─── SDK Hosts (apps outside this repo that install @nannos/embed-sdk) ──
#
# Inside the monorepo, console-frontend consumes the SDK through the npm
# workspace: always the checkout, built from source into its image at the same
# commit `just release` tags. Nothing to link there.
#
# Apps in OTHER repos (the cockpit frontend) install the published package. For
# local development of both at once, `host-link` swaps the installed copy for a
# symlink to packages/embed-sdk — package.json and the lockfile stay untouched, so
# no local path can leak into a commit, and `npm install` (or `host-unlink`)
# restores the registry copy. After `just release` publishes a new SDK version,
# `host-bump` moves each host onto it: range in package.json, lockfile, and
# node_modules. `just release` runs that for every registered host.
#
# Register a host once per machine (gitignored symlink to the dir with the package.json):
#   mkdir -p hosts && ln -s /path/to/rcplus-alloy-cockpit-frontend/app hosts/cockpit

# Show how each registered host consumes @nannos/embed-sdk (package.json ↔ lockfile ↔ node_modules)
hosts:
    #!/usr/bin/env bash
    set -euo pipefail
    source scripts/release-helpers.sh
    source scripts/host-helpers.sh
    CYAN='\033[1;36m' GREEN='\033[1;32m' RED='\033[1;31m' DIM='\033[2m' YELLOW='\033[1;33m' RESET='\033[0m'

    printf "${CYAN}%s${RESET} checkout: %s\n" "$SDK_NAME" "$(sdk_checkout_summary)"
    NAMES="$(host_names)"
    if [[ -z "$NAMES" ]]; then
      printf "${DIM}No hosts registered. Register one with:${RESET}\n"
      printf "${DIM}  mkdir -p hosts && ln -s /path/to/rcplus-alloy-cockpit-frontend/app hosts/cockpit${RESET}\n"
      exit 0
    fi

    SDK_VERSION="$(get_package_version "$SDK_PKG")"
    for name in $NAMES; do
      echo ""
      DIR="$(host_dir "$name")" || continue
      RANGE="$(host_range "$DIR")"
      INSTALLED="$(host_installed "$DIR")"
      LOCKED="$(host_locked "$DIR")"
      printf "${GREEN}● %s${RESET}  ${DIM}%s${RESET}\n" "$name" "$DIR"
      printf "   package.json   %s\n" "${RANGE:-—}"
      case "$INSTALLED" in
        linked*)   printf "   node_modules   ${YELLOW}linked${RESET} → %s\n" "${INSTALLED#linked }" ;;
        registry*) printf "   node_modules   registry v%s\n" "${INSTALLED#registry }" ;;
        *)         printf "   node_modules   ${RED}missing${RESET}\n" ;;
      esac
      case "$LOCKED" in
        registry*)   printf "   lockfile       v%s\n" "${LOCKED#registry }" ;;
        file*)       printf "   lockfile       ${RED}%s${RESET}\n" "$LOCKED" ;;
        *)           printf "   lockfile       ${RED}%s${RESET}\n" "$LOCKED" ;;
      esac

      # Verdict: what, if anything, the developer has to do next.
      if [[ -z "$RANGE" ]]; then
        printf "   ${RED}✗ no dependency on %s in package.json${RESET}\n" "$SDK_NAME"
        continue
      fi
      case "$LOCKED" in
        file*)
          printf "   ${RED}✗ the lockfile pins a local path — CI cannot install it.${RESET}  Fix: ${DIM}just host-bump %s${RESET}\n" "$name"
          continue ;;
        registry*)
          LOCKED_V="${LOCKED#registry }"
          if [[ "$(host_range_satisfied "$DIR" "$RANGE" "$LOCKED_V")" == "false" ]]; then
            printf "   ${RED}✗ lockfile v%s does not satisfy %s.${RESET}  Fix: ${DIM}just host-bump %s${RESET}\n" "$LOCKED_V" "$RANGE" "$name"
            continue
          fi ;;
        *)
          printf "   ${RED}✗ %s not in the lockfile.${RESET}  Fix: ${DIM}just host-bump %s${RESET}\n" "$SDK_NAME" "$name"
          continue ;;
      esac
      case "$INSTALLED" in
        linked*)
          printf "   ${YELLOW}⚠ linked for development${RESET} — commits are safe (lockfile pins v%s). Back to the registry: ${DIM}just host-unlink %s${RESET}\n" "$LOCKED_V" "$name"
          if [[ "$(has_changes "$SDK_PKG")" == "true" ]]; then
            printf "   ${YELLOW}⚠ the checkout has unreleased SDK changes${RESET} — release them (${DIM}just release${RESET}) before merging host code that needs them\n"
          fi ;;
        registry*)
          if [[ "${INSTALLED#registry }" != "$LOCKED_V" ]]; then
            printf "   ${YELLOW}⚠ node_modules v%s ≠ lockfile v%s.${RESET}  Fix: ${DIM}cd %s && npm install${RESET}\n" "${INSTALLED#registry }" "$LOCKED_V" "$DIR"
          else
            printf "   ${GREEN}✓ in sync with the registry (v%s)${RESET}\n" "$LOCKED_V"
          fi
          if [[ "$SDK_VERSION" != "$LOCKED_V" ]]; then
            printf "   ${DIM}SDK checkout is v%s — after its release: just host-bump %s${RESET}\n" "$SDK_VERSION" "$name"
          fi ;;
        *)
          printf "   ${RED}✗ not installed.${RESET}  Fix: ${DIM}cd %s && npm install${RESET}\n" "$DIR" ;;
      esac
    done

# Point a host at this SDK checkout for local development (symlink swap; package.json/lockfile untouched)
host-link name:
    #!/usr/bin/env bash
    set -euo pipefail
    source scripts/release-helpers.sh
    source scripts/host-helpers.sh
    CYAN='\033[1;36m' GREEN='\033[1;32m' RED='\033[1;31m' DIM='\033[2m' YELLOW='\033[1;33m' RESET='\033[0m'

    NAME="{{ name }}"
    DIR="$(host_dir "$NAME")"
    SDK_DIR="$(pwd -P)/$(pkg_dir "$SDK_PKG")"
    MOD="${DIR}/node_modules/${SDK_NAME}"

    if [[ -z "$(host_range "$DIR")" ]]; then
      printf "${RED}❌ %s does not depend on %s${RESET}\n" "$DIR" "$SDK_NAME"; exit 1
    fi
    if [[ ! -d "${DIR}/node_modules" ]]; then
      printf "${RED}❌ %s has no node_modules — run npm install there first${RESET}\n" "$DIR"; exit 1
    fi

    # The host imports the built entry points, so a checkout without dist is unusable.
    if [[ ! -f "${SDK_DIR}/dist/index.js" ]]; then
      printf "${CYAN}🏗️  No dist in the SDK checkout — building...${RESET}\n"
      (cd "$SDK_DIR" && npm run build --silent)
    fi

    if [[ -L "$MOD" && "$(cd "$MOD" && pwd -P)" == "$SDK_DIR" ]]; then
      printf "${GREEN}✓ %s is already linked to this checkout${RESET}\n" "$NAME"
    else
      rm -rf "$MOD"
      mkdir -p "$(dirname "$MOD")"
      ln -s "$SDK_DIR" "$MOD"
      printf "${GREEN}✓ %s → %s${RESET}\n" "${MOD#${DIR}/}" "$SDK_DIR"
    fi
    echo ""
    printf "${DIM}Rebuild on save:   cd %s && npm run build:watch${RESET}\n" "$(pkg_dir "$SDK_PKG")"
    printf "${DIM}Tracked files in the host are untouched — commit freely.${RESET}\n"
    printf "${DIM}Back to the registry copy:   just host-unlink %s${RESET}\n" "$NAME"

# Restore the registry copy of @nannos/embed-sdk that the host's lockfile pins
host-unlink name:
    #!/usr/bin/env bash
    set -euo pipefail
    source scripts/release-helpers.sh
    source scripts/host-helpers.sh
    CYAN='\033[1;36m' GREEN='\033[1;32m' RED='\033[1;31m' DIM='\033[2m' YELLOW='\033[1;33m' RESET='\033[0m'

    NAME="{{ name }}"
    DIR="$(host_dir "$NAME")"
    MOD="${DIR}/node_modules/${SDK_NAME}"

    if [[ ! -L "$MOD" ]]; then
      printf "${GREEN}✓ %s is not linked (%s)${RESET}\n" "$NAME" "$(host_installed "$DIR")"; exit 0
    fi
    LOCKED="$(host_locked "$DIR")"
    case "$LOCKED" in
      registry*) ;;
      *)
        printf "${RED}❌ The lockfile does not pin a registry version (%s), so npm install has nothing to restore.${RESET}\n" "$LOCKED"
        printf "   Use: ${DIM}just host-bump %s${RESET} (needs the SDK version on the registry)\n" "$NAME"
        exit 1 ;;
    esac

    rm "$MOD"
    printf "${CYAN}📥 npm install in %s (restores %s@%s from the lockfile)...${RESET}\n" "$DIR" "$SDK_NAME" "${LOCKED#registry }"
    (cd "$DIR" && npm install --no-audit --no-fund)
    printf "${GREEN}✓ %s: %s${RESET}\n" "$NAME" "$(host_installed "$DIR")"

# Move a host onto a published SDK version (package.json range, lockfile, node_modules); default: this checkout's version
host-bump name version="":
    #!/usr/bin/env bash
    set -euo pipefail
    source scripts/release-helpers.sh
    source scripts/host-helpers.sh
    CYAN='\033[1;36m' GREEN='\033[1;32m' RED='\033[1;31m' DIM='\033[2m' YELLOW='\033[1;33m' RESET='\033[0m'

    NAME="{{ name }}"
    DIR="$(host_dir "$NAME")"
    VERSION="{{ version }}"
    [[ -n "$VERSION" ]] || VERSION="$(get_package_version "$SDK_PKG")"
    MOD="${DIR}/node_modules/${SDK_NAME}"

    SECTION="$(host_dep_section "$DIR")"
    if [[ -z "$SECTION" ]]; then
      printf "${RED}❌ %s does not depend on %s${RESET}\n" "$DIR" "$SDK_NAME"; exit 1
    fi

    if [[ "$(host_locked "$DIR")" == "registry ${VERSION}" && "$(host_installed "$DIR")" == "registry ${VERSION}" \
          && "$(host_range_satisfied "$DIR" "$(host_range "$DIR")" "$VERSION")" == "true" ]]; then
      printf "${GREEN}✓ %s already at %s@%s${RESET}\n" "$NAME" "$SDK_NAME" "$VERSION"; exit 0
    fi

    printf "${CYAN}📦 %s → %s@%s${RESET}\n" "$NAME" "$SDK_NAME" "$VERSION"
    wait_for_npm_version "$VERSION" 90

    WAS_LINKED=false
    if [[ -L "$MOD" ]]; then
      rm "$MOD"
      WAS_LINKED=true
    fi

    # `npm install <name>@<version>` pins exactly that version in the lockfile and
    # writes the range in the host's own save style (caret unless its .npmrc says
    # otherwise). Editing package.json by hand and running a bare `npm install`
    # would let npm pick the newest version the range allows instead.
    SAVE_FLAG=""
    [[ "$SECTION" == "devDependencies" ]] && SAVE_FLAG="--save-dev"
    printf "${CYAN}📥 npm install %s@%s in %s...${RESET}\n" "$SDK_NAME" "$VERSION" "$DIR"
    (cd "$DIR" && npm install $SAVE_FLAG "${SDK_NAME}@${VERSION}" --no-audit --no-fund)

    INSTALLED="$(host_installed "$DIR")"
    LOCKED="$(host_locked "$DIR")"
    if [[ "$INSTALLED" != "registry ${VERSION}" || "$LOCKED" != "registry ${VERSION}" ]]; then
      printf "${RED}❌ Expected %s@%s from the registry, got: node_modules '%s', lockfile '%s'${RESET}\n" "$SDK_NAME" "$VERSION" "$INSTALLED" "$LOCKED"
      exit 1
    fi
    printf "   package.json   %s\n   lockfile       v%s\n   node_modules   registry v%s\n" "$(host_range "$DIR")" "$VERSION" "$VERSION"
    printf "${GREEN}✓ %s is on %s@%s${RESET}\n" "$NAME" "$SDK_NAME" "$VERSION"

    echo ""
    if git -C "$DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      git -C "$DIR" status --short -- package.json package-lock.json | sed 's/^/   /'
      printf "${DIM}Commit it in the host:  git -C %s commit -am \"chore: bump %s to %s\"${RESET}\n" "$DIR" "$SDK_NAME" "$VERSION"
    fi
    if [[ "$WAS_LINKED" == "true" ]]; then
      printf "${YELLOW}The host now runs the registry copy. Keep developing against the checkout with:  just host-link %s${RESET}\n" "$NAME"
    fi

# ─── Docker Build & Push ──────────────────────────────────────────

# Build Docker images for all buildable packages (optionally push: just push=true build)
build:
    #!/usr/bin/env bash
    set -euo pipefail
    BUILDABLE="{{ _buildable_packages }}"
    for pkg in $BUILDABLE; do
      just tag="{{ tag }}" push="{{ push }}" all_archs="{{ all_archs }}" build-pkg "$pkg"
    done

# Build a single package's Docker image (optionally push)
build-pkg pkg:
    #!/usr/bin/env bash
    set -euo pipefail
    source scripts/release-helpers.sh

    CYAN='\033[1;36m' GREEN='\033[1;32m' RED='\033[1;31m' DIM='\033[2m' YELLOW='\033[1;33m' RESET='\033[0m'
    tmux_tip
    PKG="{{ pkg }}"
    DO_PUSH="{{ push }}"

    if [[ "{{ all_archs }}" == "true" ]]; then
      PLATFORM="linux/amd64,linux/arm64"
    else
      PLATFORM="{{ platform }}"
    fi

    IMAGE=$(just pkg-image "$PKG")
    if [[ -z "$IMAGE" ]]; then
      echo "❌ Package '$PKG' has no Dockerfile (no image to build)"
      echo "   Buildable: {{ _buildable_packages }}"
      exit 1
    fi

    DIR="$(pkg_dir "$PKG")"
    if [[ -n "{{ tag }}" ]]; then
      TAG="{{ tag }}"
    else
      TAG="v$(get_package_version "$PKG")"
    fi

    LOGFILE=$(mktemp /tmp/nannos-build-XXXXXX)
    trap 'printf "${RED}❌ Build failed.${RESET} Full log: ${DIM}%s${RESET}\n" "$LOGFILE"; tail -20 "$LOGFILE"; exit 1' ERR

    # Shared packages as additional build contexts (no copying into pkg dir)
    BUILD_CTX_ARGS=(
      --build-context "ringier-a2a-sdk=packages/ringier-a2a-sdk"
      --build-context "agent-common=packages/agent-common"
      --build-context "object-storage=packages/object-storage"
      --build-context "embed-sdk=packages/embed-sdk"
      --build-context "workspace-root=."
    )

    # Multi-stage target support: some packages build a specific Dockerfile stage
    TARGET_ARGS=()
    case "$PKG" in
      console-backend) TARGET_ARGS=(--target api) ;;
      catalog-worker)  TARGET_ARGS=(--target catalog-worker) ;;
    esac

    printf "${CYAN}🏗️  Building %s (%s)...${RESET}" "$PKG" "$TAG"
    T=$SECONDS

    build_with_pane "$PKG" "$LOGFILE" \
      docker buildx build --platform "$PLATFORM" \
      "${BUILD_CTX_ARGS[@]}" \
      ${TARGET_ARGS[@]+"${TARGET_ARGS[@]}"} \
      -t "${IMAGE}:${TAG}" "${DIR}"

    printf "${GREEN} ✓${RESET}${DIM} (%ss)${RESET}\n" "$((SECONDS-T))"

    if [[ "$DO_PUSH" == "true" ]]; then
      printf "${CYAN}   Pushing...${RESET}"
      T=$SECONDS

      trap 'printf "${RED}❌ Push failed.${RESET} Full log: ${DIM}%s${RESET}\n" "$LOGFILE"; tail -20 "$LOGFILE"; exit 1' ERR

      build_with_pane "$PKG" "$LOGFILE" \
        docker buildx build --platform "$PLATFORM" \
        "${BUILD_CTX_ARGS[@]}" \
        ${TARGET_ARGS[@]+"${TARGET_ARGS[@]}"} \
        -t "${IMAGE}:${TAG}" --push "${DIR}"

      printf "${GREEN} ✓${RESET}${DIM} (%ss)${RESET}\n" "$((SECONDS-T))"

      # Check GHCR package visibility after first push
      IMAGE_NAME="${IMAGE##*/}"
      GHCR_ORG="{{ registry }}"; GHCR_ORG="${GHCR_ORG##*/}"
      VISIBILITY=$(gh api "/orgs/${GHCR_ORG}/packages/container/${IMAGE_NAME}" --jq '.visibility' 2>/dev/null || echo "unknown")
      if [[ "$VISIBILITY" != "public" ]]; then
        SETTINGS_URL="https://github.com/orgs/${GHCR_ORG}/packages/container/${IMAGE_NAME}/settings"
        printf "\n${YELLOW}⚠️  Package %s is not public (it is %s). Configure it at:${RESET}\n" "$IMAGE_NAME" "$VISIBILITY"
        printf "   ${DIM}%s${RESET}\n" "$SETTINGS_URL"
        printf "   ${YELLOW}1.${RESET} Under Danger Zone, change visibility to ${GREEN}Public${RESET}\n"
        printf "   ${YELLOW}2.${RESET} Under Manage access, add team ${GREEN}proj-nannos${RESET} with role ${GREEN}Write${RESET}\n"
      fi
    fi
    printf "${GREEN}✅ ${IMAGE}:${TAG}${RESET}\n"

    rm -f "$LOGFILE"

# Builds and pushes a dev prerelease image (v<version>-next.<build_ts>) for a single package
build-dev pkg:
    #!/usr/bin/env bash
    set -euo pipefail
    source scripts/release-helpers.sh
    VERSION="$(get_package_version "{{ pkg }}")"
    TAG="v${VERSION}-next.{{ build_ts }}"
    just tag="$TAG" push=true build-pkg "{{ pkg }}"

# Map package name → k8s deployment name
[private]
pkg-deploy pkg:
    #!/usr/bin/env bash
    case "{{ pkg }}" in
      console|console-backend|console-frontend) echo "console" ;;
      client-slack|client-slack-frontend) echo "client-slack" ;;
      *) echo "{{ pkg }}" ;;
    esac

# Expand a deploy group name to its constituent buildable packages
[private]
pkg-group pkg:
    #!/usr/bin/env bash
    case "{{ pkg }}" in
      console) echo "console-backend console-frontend" ;;
      client-slack) echo "client-slack client-slack-frontend" ;;
      *) echo "{{ pkg }}" ;;
    esac

# Builds and pushes dev images for ALL packages in parallel, then reconciles Flux once
deploy-dev-all:
    #!/usr/bin/env bash
    set -euo pipefail
    CYAN='\033[1;36m' GREEN='\033[1;32m' DIM='\033[2m' RED='\033[1;31m' YELLOW='\033[1;33m' RESET='\033[0m'

    source scripts/release-helpers.sh
    BUILDABLE="{{ _buildable_packages }}"
    TS="{{ build_ts }}"
    PIDS=()
    PKGS=()
    LOGS=()
    FAILED=()

    LOG_DIR=$(mktemp -d /tmp/nannos-deploy-dev-XXXXXX)
    trap 'rm -rf "$LOG_DIR"' EXIT

    # Phase 1: Build & push all packages in parallel (output silenced per-package)
    printf "${CYAN}🚀 Building & pushing all packages in parallel...${RESET}\n"
    for pkg in $BUILDABLE; do
      VERSION="$(get_package_version "$pkg")"
      TAG="v${VERSION}-next.${TS}"
      LOGFILE="${LOG_DIR}/${pkg}.log"
      printf "${DIM}   %s → %s${RESET}\n" "$pkg" "$TAG"
      just tag="$TAG" push=true build-pkg "$pkg" > "$LOGFILE" 2>&1 &
      PIDS+=($!)
      PKGS+=("$pkg")
      LOGS+=("$LOGFILE")
    done

    # Wait for all builds, report progress
    for i in "${!PIDS[@]}"; do
      if wait "${PIDS[$i]}"; then
        printf "${GREEN}   ✓ %s${RESET}\n" "${PKGS[$i]}"
      else
        FAILED+=("${PKGS[$i]}")
        printf "${RED}   ✗ %s${RESET}\n" "${PKGS[$i]}"
      fi
    done

    if [[ ${#FAILED[@]} -gt 0 ]]; then
      printf "\n${RED}❌ Failed to build %d package(s):${RESET}\n" "${#FAILED[@]}"
      for pkg in "${FAILED[@]}"; do
        printf "\n${YELLOW}── %s (last 30 lines) ──${RESET}\n" "$pkg"
        tail -30 "${LOG_DIR}/${pkg}.log"
      done
      printf "\n${DIM}Full logs: %s${RESET}\n" "$LOG_DIR"
      trap - EXIT  # keep logs around for inspection
      exit 1
    fi
    printf "${GREEN}✅ All images built & pushed${RESET}\n\n"

    # Phase 2: Reconcile Flux (single pass)
    printf "${CYAN}🔄 Reconciling Flux image repositories & policies...${RESET}\n"
    for pkg in $BUILDABLE; do
      FLUX_NAME="nannos-${pkg}"
      flux reconcile image repository "$FLUX_NAME" > /dev/null 2>&1 &
    done
    wait
    for pkg in $BUILDABLE; do
      FLUX_NAME="nannos-${pkg}"
      flux reconcile image policy "$FLUX_NAME" > /dev/null 2>&1 &
    done
    wait
    flux reconcile kustomization nannos-app --with-source > /dev/null 2>&1
    printf "${GREEN}✅ Flux reconciled${RESET}\n\n"

    # Phase 3: Wait for rollouts
    printf "${CYAN}⏳ Waiting for rollouts...${RESET}\n"
    DEPLOYS=()
    for pkg in $BUILDABLE; do
      DEPLOY=$(just pkg-deploy "$pkg")
      # Deduplicate (console-backend & console-frontend share "console")
      if [[ ! " ${DEPLOYS[*]:-} " =~ " $DEPLOY " ]]; then
        DEPLOYS+=("$DEPLOY")
      fi
    done
    ROLLOUT_FAILED=()
    ROLLOUT_PIDS=()
    ROLLOUT_NAMES=()
    for deploy in "${DEPLOYS[@]}"; do
      kubectl -n nannos rollout status "deployment/$deploy" --timeout=300s > /dev/null 2>&1 &
      ROLLOUT_PIDS+=($!)
      ROLLOUT_NAMES+=("$deploy")
    done
    for i in "${!ROLLOUT_PIDS[@]}"; do
      if wait "${ROLLOUT_PIDS[$i]}"; then
        printf "${GREEN}   ✓ %s${RESET}\n" "${ROLLOUT_NAMES[$i]}"
      else
        ROLLOUT_FAILED+=("${ROLLOUT_NAMES[$i]}")
        printf "${RED}   ✗ %s${RESET}\n" "${ROLLOUT_NAMES[$i]}"
      fi
    done
    if [[ ${#ROLLOUT_FAILED[@]} -gt 0 ]]; then
      printf "\n${RED}❌ Rollout failed for: %s${RESET}\n" "${ROLLOUT_FAILED[*]}"
      printf "${DIM}   Inspect with: kubectl -n nannos describe deployment/<name>${RESET}\n"
      exit 1
    fi
    printf "${GREEN}✅ All deployments rolled out successfully${RESET}\n"

# Builds, pushes a dev image, then triggers Flux to deploy it and waits for rollout
deploy-dev pkg:
    #!/usr/bin/env bash
    set -euo pipefail
    CYAN='\033[1;36m' GREEN='\033[1;32m' DIM='\033[2m' RED='\033[1;31m' RESET='\033[0m'

    source scripts/release-helpers.sh
    TS="{{ build_ts }}"
    PACKAGES=$(just pkg-group "{{ pkg }}")

    # Phase 1: Build & push all packages in the group
    for p in $PACKAGES; do
      VERSION="$(get_package_version "$p")"
      TAG="v${VERSION}-next.${TS}"
      IMAGE=$(just pkg-image "$p")

      just tag="$TAG" push=true build-pkg "$p"

      # Wait for the tag to be visible in the registry before triggering Flux
      printf "${CYAN}⏳ Waiting for %s:%s to be available in registry...${RESET}" "$IMAGE" "$TAG"
      for i in $(seq 1 30); do
        if docker manifest inspect "${IMAGE}:${TAG}" > /dev/null 2>&1; then
          printf "${GREEN} ✓${RESET}\n"
          break
        fi
        if [[ $i -eq 30 ]]; then
          printf "\n${RED}❌ Tag %s not visible in registry after 30s. Proceeding anyway...${RESET}\n" "$TAG"
        fi
        sleep 1
      done
    done

    # Phase 2: Reconcile Flux for all packages in the group
    for p in $PACKAGES; do
      FLUX_NAME="nannos-${p}"
      flux reconcile image repository "$FLUX_NAME"
      flux reconcile image policy "$FLUX_NAME"
    done
    flux reconcile kustomization nannos-app --with-source

    # Phase 3: Wait for rollout (deduplicated — group members share a deployment)
    DEPLOY=$(just pkg-deploy "{{ pkg }}")
    printf "${CYAN}⏳ Waiting for deployment/%s rollout...${RESET}\n" "$DEPLOY"
    kubectl -n nannos rollout status "deployment/$DEPLOY" --timeout=300s
    printf "${GREEN}✅ deployment/%s rolled out successfully${RESET}\n" "$DEPLOY"

# Updates the prod image tag in the gitops repo, commits and pushes (FluxCD picks it up)
deploy-prod pkg="":
    #!/usr/bin/env bash
    set -euo pipefail
    CYAN='\033[1;36m' GREEN='\033[1;32m' DIM='\033[2m' RED='\033[1;31m' YELLOW='\033[1;33m' RESET='\033[0m'

    source scripts/release-helpers.sh
    GITOPS_DIR="gitops"
    PROD_PATCH="${GITOPS_DIR}/manifests/apps/nannos/prod/image-patch.yaml"

    # Validate gitops symlink
    if [[ ! -d "$GITOPS_DIR" ]]; then
      printf "${RED}❌ gitops directory not found.${RESET}\n"
      printf "   Create a symlink: ${DIM}ln -s /path/to/gitops-repo gitops${RESET}\n"
      exit 1
    fi

    if [[ ! -f "$PROD_PATCH" ]]; then
      printf "${RED}❌ Prod image patch not found: %s${RESET}\n" "$PROD_PATCH"
      exit 1
    fi

    # Validate gitops repo is on main and up-to-date
    pushd "$GITOPS_DIR" > /dev/null
    CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
    if [[ "$CURRENT_BRANCH" != "main" ]]; then
      printf "${RED}❌ gitops repo is on branch '%s', expected 'main'${RESET}\n" "$CURRENT_BRANCH"
      exit 1
    fi
    if ! git diff --quiet || ! git diff --cached --quiet; then
      printf "${RED}❌ gitops repo has uncommitted changes${RESET}\n"
      printf "   Run: ${DIM}cd %s && git status${RESET}\n" "$GITOPS_DIR"
      exit 1
    fi
    git fetch origin main --quiet
    LOCAL_SHA=$(git rev-parse HEAD)
    REMOTE_SHA=$(git rev-parse origin/main)
    if [[ "$LOCAL_SHA" != "$REMOTE_SHA" ]]; then
      printf "${RED}❌ gitops repo is not up-to-date with origin/main${RESET}\n"
      printf "   Local:  %s\n" "$LOCAL_SHA"
      printf "   Remote: %s\n" "$REMOTE_SHA"
      printf "   Run: ${DIM}cd %s && git pull${RESET}\n" "$GITOPS_DIR"
      exit 1
    fi
    popd > /dev/null

    # Determine packages to deploy
    if [[ -n "{{ pkg }}" ]]; then
      PACKAGES=("{{ pkg }}")
    else
      PACKAGES=({{ _buildable_packages }})
    fi

    DEPLOYED=()
    for PKG in "${PACKAGES[@]}"; do
      IMAGE=$(just pkg-image "$PKG")
      if [[ -z "$IMAGE" ]]; then
        printf "${YELLOW}⚠️  Skipping %s — no Docker image${RESET}\n" "$PKG"
        continue
      fi

      VERSION="v$(get_package_version "$PKG")"

      # Verify the image exists in the registry
      printf "${CYAN}🔍 Verifying %s:%s exists in registry...${RESET}" "$IMAGE" "$VERSION"
      if ! docker manifest inspect "${IMAGE}:${VERSION}" > /dev/null 2>&1; then
        printf "\n${RED}❌ Image %s:%s not found in registry. Skipping.${RESET}\n" "$IMAGE" "$VERSION"
        continue
      fi
      printf "${GREEN} ✓${RESET}\n"

      # Update the image tag in prod overlay
      printf "${CYAN}📝 Updating %s → %s in prod overlay...${RESET}" "$PKG" "$VERSION"
      sed -i '' "s|image: ${IMAGE}:.*|image: ${IMAGE}:${VERSION}|g" "$PROD_PATCH"
      printf "${GREEN} ✓${RESET}\n"
      DEPLOYED+=("${PKG} ${VERSION}")
    done

    # Commit and push in gitops repo
    cd "$GITOPS_DIR"
    if git diff --quiet; then
      printf "${YELLOW}⚠️  No changes — all packages already at current versions in prod${RESET}\n"
      exit 0
    fi

    if [[ ${#DEPLOYED[@]} -eq 1 ]]; then
      COMMIT_MSG="deploy: ${DEPLOYED[0]} to prod"
    else
      COMMIT_MSG="deploy: $(IFS=', '; echo "${DEPLOYED[*]}") to prod"
    fi

    git add -A
    git commit -m "$COMMIT_MSG"
    printf "${CYAN}🚀 Pushing to gitops repo...${RESET}"
    git push
    printf "${GREEN} ✓${RESET}\n"
    printf "${GREEN}✅ Deployed to prod: %s${RESET}\n" "$(IFS=', '; echo "${DEPLOYED[*]}")"

# ─── Local Database ───────────────────────────────────────────────

LOCAL_DB_PORT := "4700"
LOCAL_DB_DATA := ".local-db-data"
TEST_DB_PORT  := "4000"

# Migration image (built locally from sqlmigrations package)
_migrations_image := "nannos-migrations:local"
_migrations_dir := "packages/orchestrator-agent/sqlmigrations"

# Start local postgres for development (persistent data)
local-db:
  #!/usr/bin/env bash
  set -e
  mkdir -p {{LOCAL_DB_DATA}}

  if docker ps --filter publish={{LOCAL_DB_PORT}} --format '{{{{.Names}}}}' | grep -q .; then
    echo "✓ PostgreSQL already running on port {{LOCAL_DB_PORT}}"
    exit 0
  fi

  docker ps -aq --filter name=nannos-local-db | xargs -r docker rm -f 2>/dev/null || true

  echo "Starting PostgreSQL 18 on port {{LOCAL_DB_PORT}}..."
  docker run -d \
    --name nannos-local-db \
    -p {{LOCAL_DB_PORT}}:5432 \
    -v "$(pwd)/{{LOCAL_DB_DATA}}:/var/lib/postgresql" \
    -e POSTGRES_USER=postgres \
    -e POSTGRES_PASSWORD=password \
    -e POSTGRES_DB=nannos \
    pgvector/pgvector:pg18

  echo "Waiting for PostgreSQL to be ready..."
  until docker exec nannos-local-db pg_isready -U postgres > /dev/null 2>&1; do
    sleep 0.5
  done
  echo "✓ PostgreSQL is ready on port {{LOCAL_DB_PORT}}"

# Start a disposable test postgres (no persistent data)
_start-test-db:
  #!/usr/bin/env bash
  set -e

  if docker ps --filter publish={{TEST_DB_PORT}} --format '{{{{.Names}}}}' | grep -q .; then
    echo "✓ Test PostgreSQL already running on port {{TEST_DB_PORT}}"
    exit 0
  fi

  docker ps -aq --filter name=nannos-test-db | xargs -r docker rm -f 2>/dev/null || true

  echo "Starting test PostgreSQL 18 on port {{TEST_DB_PORT}}..."
  docker run -d \
    --name nannos-test-db \
    -p {{TEST_DB_PORT}}:5432 \
    -e POSTGRES_USER=postgres \
    -e POSTGRES_PASSWORD=password \
    -e POSTGRES_DB=nannos \
    pgvector/pgvector:pg18

  echo "Waiting for PostgreSQL to be ready..."
  until docker exec nannos-test-db pg_isready -U postgres > /dev/null 2>&1; do
    sleep 0.5
  done
  echo "✓ Test PostgreSQL is ready on port {{TEST_DB_PORT}}"

# Build the migrations image locally
[private]
_build-migrations:
  #!/usr/bin/env bash
  set -e
  docker build -t {{_migrations_image}} {{_migrations_dir}}

# Run migrations against a given port
[private]
_run-migrations port: _build-migrations
  #!/usr/bin/env bash
  set -e
  # Ensure the target schema exists (Rambler assumes it does)
  docker run --rm \
    --network host \
    --entrypoint psql \
    -e PGPASSWORD=password \
    {{_migrations_image}} \
    -h 127.0.0.1 -p {{port}} -U postgres -d nannos \
    -c "CREATE SCHEMA IF NOT EXISTS nannos;"
  docker run --rm \
    --network host \
    -v "$(pwd)/{{_migrations_dir}}/ddl:/migrations/ddl:ro" \
    -e PGHOST=127.0.0.1 \
    -e PGPORT={{port}} \
    -e PGUSER=postgres \
    -e PGPASSWORD=password \
    -e PGDATABASE=nannos \
    -e PGSCHEMA=nannos \
    -e RAMBLER_SSLMODE=disable \
    {{_migrations_image}}

# Start test db and run migrations
test-db: _start-test-db
  #!/usr/bin/env bash
  set -e
  echo "Running migrations against test db (port {{TEST_DB_PORT}})..."
  just _run-migrations {{TEST_DB_PORT}}
  echo "✓ Test db ready with migrations on port {{TEST_DB_PORT}}"

# Reset local dev database (deletes all data)
reset-db:
  #!/usr/bin/env bash
  set -e
  docker ps -q --filter name=nannos-local-db | xargs -r docker stop
  docker ps -aq --filter name=nannos-local-db | xargs -r docker rm
  rm -rf {{LOCAL_DB_DATA}}
  echo "✓ Dev database cleared. Run 'just local-db' to start fresh."

# Reset test database (stop & remove container)
reset-test-db:
  #!/usr/bin/env bash
  set -e
  docker ps -q --filter name=nannos-test-db | xargs -r docker stop
  docker ps -aq --filter name=nannos-test-db | xargs -r docker rm
  echo "✓ Test database cleared. Run 'just test-db' to start fresh."

# Connect to local dev database via psql
psql: local-db
  PGPASSWORD=password psql -h localhost -p {{LOCAL_DB_PORT}} -U postgres -d nannos

# Connect to test database via psql
test-db-psql: test-db
  PGPASSWORD=password psql -h localhost -p {{TEST_DB_PORT}} -U postgres -d nannos

# ─── Local Development ────────────────────────────────────────────

# Start all services locally (requires OPENAI_COMPATIBLE_BASE_URL)
start-local *FLAGS:
  ./scripts/start-local.sh {{FLAGS}}

# Stop local infrastructure (PostgreSQL + Keycloak) and all services
stop-local:
  tmux kill-session -t nannos 2>/dev/null || true
  docker rm -f nannos-litellm-proxy-local 2>/dev/null || true
  cd scripts/local-dev && docker compose down

# Stop local infrastructure and delete all data
reset-local:
  tmux kill-session -t nannos 2>/dev/null || true
  docker rm -f nannos-litellm-proxy-local 2>/dev/null || true
  cd scripts/local-dev && docker compose down -v
  @echo "✓ Local infrastructure removed. Run 'just start-local' to start fresh."

recon: # Reconcile local Kubernetes cluster with Flux (for testing manifests)
  #!/usr/bin/env bash
  for name in $(kubectl get imagerepository -n flux-system -o jsonpath='{.items[*].metadata.name}'); do
    (flux reconcile image repository "$name" 2>&1 | sed "s/^/[$name] /" && flux reconcile image policy "$name" 2>&1 | sed "s/^/[$name] /") &
  done
  wait
  flux reconcile kustomization nannos-app --with-source

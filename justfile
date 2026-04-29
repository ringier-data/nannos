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
#   just build                              → build Docker images for all buildable packages
#   just push=true build                    → build & push Docker images
#   just build-pkg orchestrator-agent       → build a single package image
#   just push=true build-pkg orchestrator-agent → build & push a single package image

# ─── Configuration ─────────────────────────────────────────────────

# TODO: set your container registry
registry := "ghcr.io/ringier-data"

# Per-package image names (only packages with Dockerfiles)
img_agent_creator    := registry + "/nannos-agent-creator"
img_agent_runner     := registry + "/nannos-agent-runner"
img_orchestrator     := registry + "/nannos-orchestrator-agent"
img_console_backend  := registry + "/nannos-console-backend"
img_console_frontend := registry + "/nannos-console-frontend"
img_client_slack := registry + "/nannos-client-slack"
img_client_slack_frontend := registry + "/nannos-client-slack-frontend"
img_client_email := registry + "/nannos-client-email"
img_voice_agent := registry + "/nannos-voice-agent"
img_catalog_worker := registry + "/nannos-catalog-worker"

# Default build platform
platform := "linux/arm64"

# Timestamp for dev prerelease suffix (YYYYMMDDHHmmss UTC)
build_ts := `date -u +%Y%m%d%H%M%S`

# Packages that have Dockerfiles (used by build recipes)
_buildable_packages := "agent-creator agent-runner orchestrator-agent console-backend catalog-worker console-frontend client-slack client-slack-frontend client-email voice-agent"

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
      agent-creator)      echo "{{ img_agent_creator }}" ;;
      agent-runner)       echo "{{ img_agent_runner }}" ;;
      orchestrator-agent) echo "{{ img_orchestrator }}" ;;
      console-backend)    echo "{{ img_console_backend }}" ;;
      console-frontend)   echo "{{ img_console_frontend }}" ;;
      client-slack)       echo "{{ img_client_slack }}" ;;
      client-slack-frontend) echo "{{ img_client_slack_frontend }}" ;;
      client-email)       echo "{{ img_client_email }}" ;;
      voice-agent)        echo "{{ img_voice_agent }}" ;;
      catalog-worker)     echo "{{ img_catalog_worker }}" ;;
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
    set -euo pipefail
    source scripts/release-helpers.sh

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

    # Phase 3: Push all (reuses cached builds)
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

    # Phase 4: Push git commit and tags to remote
    trap - ERR  # Clear rollback — images are already pushed
    printf "${CYAN}🚀 Pushing release commit and tags...${RESET}"
    git push && git push --tags
    printf "${GREEN} ✓${RESET}\n"

# Release a single package (bump version, commit, tag, build, push)
release-pkg pkg bump="":
    #!/usr/bin/env bash
    set -euo pipefail
    source scripts/release-helpers.sh

    CYAN='\033[1;36m' GREEN='\033[1;32m' RED='\033[1;31m' DIM='\033[2m' YELLOW='\033[1;33m' RESET='\033[0m'
    PKG="{{ pkg }}"
    BUMP="{{ bump }}"

    # Validate package name
    if [[ ! " $ALL_PACKAGES " =~ " $PKG " ]]; then
      echo "❌ Unknown package: $PKG"
      echo "   Available: $ALL_PACKAGES"
      exit 1
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

    # Phase 3: Push (reuses cached build)
    if [[ -n "$IMAGE" ]]; then
      just push=true build-pkg "$PKG"
    fi

    # Phase 4: Push git commit and tag to remote
    trap - ERR  # Clear rollback — image is already pushed
    printf "${CYAN}🚀 Pushing release commit and tag...${RESET}"
    git push && git push --tags
    printf "${GREEN} ✓${RESET}\n"

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

    CYAN='\033[1;36m' GREEN='\033[1;32m' RED='\033[1;31m' DIM='\033[2m' RESET='\033[0m'
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
      console-backend|console-frontend) echo "console" ;;
      client-slack|client-slack-frontend) echo "client-slack" ;;
      *) echo "{{ pkg }}" ;;
    esac

# Builds, pushes a dev image, then triggers Flux to deploy it and waits for rollout
deploy-dev pkg:
    #!/usr/bin/env bash
    set -euo pipefail
    CYAN='\033[1;36m' GREEN='\033[1;32m' DIM='\033[2m' RED='\033[1;31m' RESET='\033[0m'

    source scripts/release-helpers.sh
    VERSION="$(get_package_version "{{ pkg }}")"
    TAG="v${VERSION}-next.{{ build_ts }}"
    IMAGE=$(just pkg-image "{{ pkg }}")

    # Build & push with the computed tag
    just tag="$TAG" push=true build-pkg "{{ pkg }}"

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

    FLUX_NAME="nannos-{{ pkg }}"
    flux reconcile image repository "$FLUX_NAME"
    flux reconcile image policy "$FLUX_NAME"
    flux reconcile kustomization nannos-app --with-source

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
  cd scripts/local-dev && docker compose down

# Stop local infrastructure and delete all data
reset-local:
  tmux kill-session -t nannos 2>/dev/null || true
  cd scripts/local-dev && docker compose down -v
  @echo "✓ Local infrastructure removed. Run 'just start-local' to start fresh."

recon: # Reconcile local Kubernetes cluster with Flux (for testing manifests)
  #!/usr/bin/env bash
  for name in $(kubectl get imagerepository -n flux-system -o jsonpath='{.items[*].metadata.name}'); do
    (flux reconcile image repository "$name" 2>&1 | sed "s/^/[$name] /" && flux reconcile image policy "$name" 2>&1 | sed "s/^/[$name] /") &
  done
  wait
  flux reconcile kustomization nannos-app --with-source

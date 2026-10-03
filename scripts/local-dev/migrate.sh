#!/usr/bin/env bash
set -euo pipefail

# Setup steps `migrate-console` / `migrate-docstore` of process-compose.yaml: build the database's
# migration image and apply its migrations (Rambler) to this stack's database.
#
#   migrate.sh console|docstore
#
# Reads the stack env start-local.sh exports: NANNOS_ROOT, NANNOS_SLOT, NANNOS_STACK_DIR.
# A slot (1-8) creates its database on first use and refuses to start when a migration it already
# applied has been edited since (Rambler records names, not contents): 'just db-reset' rebuilds.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=slot-common.sh
source "$SCRIPT_DIR/slot-common.sh"

ROOT="$NANNOS_ROOT"
SLOT="$NANNOS_SLOT"
case "${1:-}" in
  console)
    CONTAINER="$PG_CONSOLE_CONTAINER"; PORT=5401; PKG=console-backend
    IMAGE="nannos-console-migrations" ;;
  docstore)
    CONTAINER="$PG_DOCSTORE_CONTAINER"; PORT=5402; PKG=orchestrator-agent
    IMAGE="nannos-docstore-migrations" ;;
  *) echo "usage: migrate.sh console|docstore" >&2; exit 2 ;;
esac
DB="$(slot_db_name "$SLOT" "$1")"
DDL="packages/$PKG/sqlmigrations/ddl"
HASHES="$NANNOS_STACK_DIR/migrations-$1.sha256"

psql_in() {  # db sql — exec reads stdin: give it none
  docker exec "$CONTAINER" psql -U postgres -d "$1" -v ON_ERROR_STOP=1 -qtAc "$2" </dev/null
}

# The image is tagged with a hash of its build context: an unchanged migrations dir reuses the
# image without a build — and without the network (BuildKit re-checks base-image metadata on
# Docker Hub even on a full cache hit, which takes minutes when the Hub is slow). It also keeps
# two checkouts with different migrations from overwriting each other's image.
CONTEXT_DIR="$ROOT/packages/$PKG/sqlmigrations"
TAG="$(cd "$CONTEXT_DIR" && find . -type f | LC_ALL=C sort | xargs shasum -a 256 | shasum -a 256 | cut -c1-16)"
IMAGE="$IMAGE:$TAG"

# Build with the current context's own docker-driver builder, whatever buildx builder is the
# default. A docker-container builder (needed for multi-platform pushes) exports and re-loads the
# whole image on every build, even a full cache hit: ~70 s vs ~7 s. The image is local-only;
# release builds keep the default builder. Docker without buildx (or with BuildKit off) has no
# --builder flag. Docker Hub answers the metadata check with a transient error now and then: retry.
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  builder_args=()
  context="$(docker context show 2>/dev/null || true)"
  if [[ -n "$context" ]] && docker buildx inspect "$context" >/dev/null 2>&1; then
    builder_args=(--builder "$context")
  fi
  for attempt in 1 2 3; do
    docker build ${builder_args[@]+"${builder_args[@]}"} --quiet -t "$IMAGE" "$CONTEXT_DIR" >/dev/null && break
    [[ $attempt == 3 ]] && { echo "✗ Building $IMAGE failed"; exit 1; }
    echo "⚠ Building $IMAGE failed (attempt $attempt); retrying"
    sleep 3
  done
fi

if [[ "$SLOT" != 0 ]]; then
  if [[ -z "$(psql_in postgres "SELECT 1 FROM pg_database WHERE datname = '$DB'")" ]]; then
    psql_in postgres "CREATE DATABASE \"$DB\""
    rm -f "$HASHES"
    echo "✓ Created database $DB"
  elif [[ -f "$HASHES" ]]; then
    # Re-applying an edited migration is not something Rambler does: the schema would drift.
    edited="$(slot_migration_hashes "$ROOT" "$DDL" \
      | awk 'NR == FNR { seen[$2] = $1; next } ($2 in seen) && seen[$2] != $1 { print $2 }' "$HASHES" -)"
    if [[ -n "$edited" ]]; then
      echo "✗ Migrations edited since slot $SLOT applied them to $DB: $(echo $edited)"
      echo "  Run 'just db-reset' to rebuild the slot's databases."
      exit 1
    fi
  fi
fi

if [[ "$1" == docstore ]]; then
  psql_in "$DB" "CREATE EXTENSION IF NOT EXISTS vector"
fi

echo "▸ Applying $PKG migrations (db: $DB, port: $PORT)..."
docker run --rm --network host --user 0 \
  -e PGHOST=127.0.0.1 -e PGPORT="$PORT" -e PGUSER=postgres -e PGPASSWORD=password \
  -e PGDATABASE="$DB" -e PGSCHEMA=public -e RAMBLER_SSLMODE=disable \
  "$IMAGE" </dev/null

if [[ "$SLOT" != 0 ]]; then
  slot_migration_hashes "$ROOT" "$DDL" > "$HASHES"
fi

if [[ "$1" == console ]]; then
  # Local dev: every user is an administrator, and test@local.dev exists (first local start).
  psql_in "$DB" "UPDATE users SET is_administrator = true WHERE is_administrator = false" || true
  psql_in "$DB" "INSERT INTO users (id, sub, email, first_name, last_name, is_administrator, role, status, created_at, updated_at)
    VALUES (gen_random_uuid(), 'local-test-user', 'test@local.dev', 'Test', 'User', true, 'admin', 'active', now(), now())
    ON CONFLICT (LOWER(email)) WHERE deleted_at IS NULL DO UPDATE SET is_administrator = true, role = 'admin'" || true
  # The Model Gateway's own tables live in a `litellm` schema of the console database (mirrors
  # the deployments' shared database). The gateway step waits for this one.
  psql_in "$DB" "CREATE SCHEMA IF NOT EXISTS litellm"

  # A slot's models come from its gateway config (models registered in another stack's
  # database are not here), but which one serves the `chat` tier lives in model_defaults —
  # empty in a new database, and agents refuse to run without it. The config's first model
  # becomes the `chat` default (order litellm-local-models.yaml accordingly); every other tier
  # falls back to `chat`. Only while `chat` is unset: a default chosen in the console stays.
  if [[ "$SLOT" != 0 && -z "$(psql_in "$DB" "SELECT 1 FROM model_defaults WHERE role = 'chat'")" ]]; then
    alias="$(sed -n 's/^[[:space:]]*-[[:space:]]*model_name:[[:space:]]*//p' "${NANNOS_GW_CONFIG:-/dev/null}" \
      | tr -d "\"'" | awk 'NR == 1 {print $1}')"
    if [[ -n "$alias" ]]; then
      psql_in "$DB" "INSERT INTO model_defaults (role, model_alias) VALUES ('chat', '$alias') ON CONFLICT (role) DO NOTHING"
      psql_in "$DB" "INSERT INTO model_alias_tiers (alias, role) VALUES ('$alias', 'chat') ON CONFLICT (alias, role) DO NOTHING"
      echo "✓ Default chat model: $alias (the gateway config's first; change it in the console under Models → Defaults)"
    else
      echo "⚠ No models in the gateway config: set the chat default in the console (Models → Defaults)"
    fi
  fi
fi
echo "✓ $DB migrated"

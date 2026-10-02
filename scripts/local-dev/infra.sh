#!/usr/bin/env bash
set -euo pipefail

# Setup step `infra` of process-compose.yaml: make sure the shared infrastructure — both
# PostgreSQL servers and the local Keycloak — runs, and wait for PostgreSQL. It only ever starts
# it: every stack (slot 0 and slots 1-8) uses the same containers, so no stack's `down` stops them
# (`just stop-local` does, for all of them).

cd "$(dirname "${BASH_SOURCE[0]}")"

# One realm path for every checkout, so the compose config (and Keycloak) is the same from any
# worktree: a path relative to each checkout changed the config hash, and every start from another
# worktree re-created Keycloak (a ~110 s cold boot) under the stacks using it. Keycloak imports the
# file only into an empty realm; keycloak-setup.sh brings a running one up to date.
export NANNOS_KEYCLOAK_REALM_FILE="${NANNOS_HOME:-$HOME/.nannos}/keycloak/realm-export.json"
mkdir -p "$(dirname "$NANNOS_KEYCLOAK_REALM_FILE")"
cmp -s keycloak/realm-export.json "$NANNOS_KEYCLOAK_REALM_FILE" || cp keycloak/realm-export.json "$NANNOS_KEYCLOAK_REALM_FILE"

docker compose up -d

for service in postgres-console postgres-docstore; do
  for _ in $(seq 1 120); do
    # exec reads stdin: give it none, so it never swallows anything meant for this script.
    docker compose exec -T "$service" pg_isready -U postgres </dev/null >/dev/null 2>&1 && break
    sleep 1
  done
  docker compose exec -T "$service" pg_isready -U postgres </dev/null >/dev/null 2>&1 \
    || { echo "✗ $service did not become ready. Check: docker compose logs $service"; exit 1; }
done
echo "✓ PostgreSQL is ready"

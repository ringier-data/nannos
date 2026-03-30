#!/bin/bash
set -e

# Migration script - runs as the database owner (app user)
# Environment variables:
#   PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE, PGSCHEMA
#   (or RAMBLER_HOST, RAMBLER_PORT, RAMBLER_USER, RAMBLER_PASSWORD, RAMBLER_DATABASE, RAMBLER_SCHEMA)

# Normalize env vars: support POSTGRES_*, RAMBLER_*, or PG* prefixes
PGHOST="${PGHOST:-${POSTGRES_HOST:-${RAMBLER_HOST:-127.0.0.1}}}"
PGPORT="${PGPORT:-${POSTGRES_PORT:-${RAMBLER_PORT:-5432}}}"
PGUSER="${PGUSER:-${POSTGRES_USER:-${RAMBLER_USER:-}}}"
PGPASSWORD="${PGPASSWORD:-${POSTGRES_PASSWORD:-${RAMBLER_PASSWORD:-}}}"
PGDATABASE="${PGDATABASE:-${POSTGRES_DB:-${RAMBLER_DATABASE:-}}}"
PGSCHEMA="${PGSCHEMA:-${POSTGRES_SCHEMA:-${RAMBLER_SCHEMA:-$PGUSER}}}"

echo "Waiting for database at $PGHOST:$PGPORT to be ready..."
MAX_RETRIES=20
RETRY_COUNT=0
while ! psql "postgresql://$PGUSER:$PGPASSWORD@$PGHOST:$PGPORT/$PGDATABASE" -c "SELECT 1" > /dev/null; do
  RETRY_COUNT=$((RETRY_COUNT + 1))
  if [ $RETRY_COUNT -ge $MAX_RETRIES ]; then
    echo "Database not ready after $MAX_RETRIES attempts. Exiting."
    exit 1
  fi
  echo "Database not ready, sleeping... (attempt $RETRY_COUNT/$MAX_RETRIES)"
  sleep 2
done

echo -n 'search_path: '
psql "postgresql://$PGUSER:$PGPASSWORD@$PGHOST:$PGPORT/$PGDATABASE" -c "show search_path"

echo "Database is ready! Starting migration on $PGDATABASE as $PGUSER..."

RAMBLER_HOST=$PGHOST \
RAMBLER_PORT=$PGPORT \
RAMBLER_DRIVER=postgresql \
RAMBLER_USER=$PGUSER \
RAMBLER_PASSWORD=$PGPASSWORD \
RAMBLER_DATABASE=$PGDATABASE \
RAMBLER_SCHEMA=$PGSCHEMA \
rambler apply -a

#!/bin/bash
set -e

# Normalize env vars: support POSTGRES_*, RAMBLER_*, or PG* prefixes
PGHOST="${PGHOST:-${POSTGRES_HOST:-${RAMBLER_HOST:-127.0.0.1}}}"
PGPORT="${PGPORT:-${POSTGRES_PORT:-${RAMBLER_PORT:-5432}}}"
PGUSER="${PGUSER:-${POSTGRES_USER:-${RAMBLER_USER:-}}}"
PGPASSWORD="${PGPASSWORD:-${POSTGRES_PASSWORD:-${RAMBLER_PASSWORD:-}}}"
PGDATABASE="${PGDATABASE:-${POSTGRES_DB:-${RAMBLER_DATABASE:-}}}"
PGSCHEMA="${PGSCHEMA:-${POSTGRES_SCHEMA:-${RAMBLER_SCHEMA:-}}}"
APP_USERNAME="${APP_USERNAME:-}"
APP_PASSWORD="${APP_PASSWORD:-}"

echo "Waiting for database at $PGHOST:$PGPORT to be ready..."
MAX_RETRIES=20
RETRY_COUNT=0
until nc -z "$PGHOST" "$PGPORT"; do
  RETRY_COUNT=$((RETRY_COUNT + 1))
  if [ $RETRY_COUNT -ge $MAX_RETRIES ]; then
    echo "Database not ready after $MAX_RETRIES attempts. Exiting."
    exit 1
  fi
  echo "Database not ready, sleeping... (attempt $RETRY_COUNT/$MAX_RETRIES)"
  sleep 2
done
echo "Database is ready!"

# Run schema setup if SETUP_SQL is provided

echo "Setting up schema and permissions..."
psql -v ON_ERROR_STOP=1 \
-v database="$PGDATABASE" \
-v schema="$PGSCHEMA" \
-v app_user="$APP_USERNAME" \
-v app_password="$APP_PASSWORD" \
"postgresql://$PGUSER:$PGPASSWORD@$PGHOST:$PGPORT/$PGDATABASE" <<EOF
    DROP SCHEMA IF EXISTS public CASCADE;
    CREATE SCHEMA IF NOT EXISTS :schema;
    CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA :schema;
    SELECT NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = :'app_user') AS should_create \gset
    \if :should_create
      CREATE USER :"app_user" WITH PASSWORD :'app_password';
    \else
      \echo 'User already exists, skipping creation'
    \endif
    GRANT CONNECT, CREATE ON DATABASE :database TO :"app_user";
    GRANT USAGE ON SCHEMA :schema TO :"app_user";
    GRANT USAGE ON ALL SEQUENCES IN SCHEMA :schema TO :"app_user";
    GRANT SELECT, UPDATE, DELETE, INSERT ON ALL TABLES IN SCHEMA :schema TO :"app_user";
    GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA :schema TO :"app_user";
    ALTER DEFAULT PRIVILEGES IN SCHEMA :schema GRANT SELECT, UPDATE, DELETE, INSERT ON TABLES TO :"app_user";
    ALTER DEFAULT PRIVILEGES IN SCHEMA :schema GRANT USAGE ON SEQUENCES TO :"app_user";
    ALTER DEFAULT PRIVILEGES IN SCHEMA :schema GRANT EXECUTE ON FUNCTIONS TO :"app_user";
    ALTER USER :"app_user" SET search_path=:'schema';
EOF

echo "Schema setup complete!"

echo "Starting migration..."

RAMBLER_HOST=$PGHOST \
RAMBLER_PORT=$PGPORT \
RAMBLER_DRIVER=postgresql \
RAMBLER_USER=$PGUSER \
RAMBLER_PASSWORD=$PGPASSWORD \
RAMBLER_DATABASE=$PGDATABASE \
RAMBLER_SCHEMA=$PGSCHEMA \
rambler apply -a

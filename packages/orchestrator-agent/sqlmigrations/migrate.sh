#!/bin/bash
set -e

# Migration script - runs as the database owner (app user)
# Environment variables:
#   PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE, PGSCHEMA

PGHOST="${PGHOST:-127.0.0.1}"
PGPORT="${PGPORT:-5432}"

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
echo "Database is ready!"

echo "Setting up schema..."
psql -v ON_ERROR_STOP=1 \
-v schema="$PGSCHEMA" \
"postgresql://$PGUSER:$PGPASSWORD@$PGHOST:$PGPORT/$PGDATABASE" <<EOF
    CREATE SCHEMA IF NOT EXISTS :schema;
    CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA :schema;
    ALTER USER CURRENT_USER SET search_path=:'schema';
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

#!/bin/sh
set -e

echo "Running database migrations..."
./migrate.sh

echo "Starting application..."
exec /usr/local/bin/node dist/src/app.js

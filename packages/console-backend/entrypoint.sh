#!/bin/bash
set -e

echo "Running database migrations..."
./migrate.sh

echo "Starting application..."
exec ./.venv/bin/uvicorn app:asgi_app --host 0.0.0.0 --port ${API_PORT} --log-config log_conf.yml --no-access-log --no-use-colors

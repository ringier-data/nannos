#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "🧹 Cleaning up any previous test containers..."
docker compose -f docker-compose.test.yml down -v 2>/dev/null || true

echo "🐘 Starting PostgreSQL and running migrations..."
docker compose -f docker-compose.test.yml up --build --abort-on-container-exit --exit-code-from migrate

EXIT_CODE=$?

echo ""
if [ $EXIT_CODE -eq 0 ]; then
  echo "✅ Migration test PASSED!"
  
  echo ""
  echo "📋 Verifying created tables..."
  docker compose -f docker-compose.test.yml exec -T postgres psql -U postgres -d testdb -c "\dt a2a_google_chat.*"
  
  echo ""
  echo "👤 Verifying app_user was created..."
  docker compose -f docker-compose.test.yml exec -T postgres psql -U postgres -d testdb -c "\du app_user"
else
  echo "❌ Migration test FAILED with exit code $EXIT_CODE"
fi

echo ""
echo "🧹 Cleaning up..."
docker compose -f docker-compose.test.yml down -v

exit $EXIT_CODE

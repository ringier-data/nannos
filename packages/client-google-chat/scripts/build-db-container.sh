#!/bin/bash

set -e

# Parse arguments
FORCE=false
CLONES=""
for arg in "$@"; do
  case $arg in
    --force|-f)
      FORCE=true
      ;;
    *)
      CLONES=$arg
      ;;
  esac
done

export RAMBLER_DRIVER=postgresql
export RAMBLER_PROTOCOL=tcp
export RAMBLER_PORT=5432
export RAMBLER_USER=postgres
export RAMBLER_PASSWORD=password
export RAMBLER_DIRECTORY=/scripts
export RAMBLER_DATABASE=nannos
export RAMBLER_TABLE=migrations
export RAMBLER_SCHEMA=nannos
export PGDATA=/var/lib/postgresql-static/data
CURRENT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export SCRIPTS=$(cd "$CURRENT_DIR/../sqlmigrations/ddl" && pwd)

## === PREPARE ===
SOFTWARE_COMPONENT=nannos-google-chat-client
IMAGE_NAME=$SOFTWARE_COMPONENT-testdb
IMAGE_TAG=$IMAGE_NAME:latest

# Calculate hash of all DDL files
DDL_HASH=$(find "$SCRIPTS" -type f -name "*.sql" -exec shasum -a 256 {} \; | sort | shasum -a 256 | cut -d' ' -f1)

# Check if image with matching hash already exists
EXISTING_HASH=$(docker inspect --format='{{index .Config.Labels "ddl.hash"}}' $IMAGE_TAG 2>/dev/null || echo "")

if [ "$FORCE" = false ] && [ "$EXISTING_HASH" = "$DDL_HASH" ]; then
  echo "✓ Database image is up to date (hash: ${DDL_HASH:0:12})"
  echo "$IMAGE_TAG"
  exit 0
fi

if [ "$FORCE" = true ]; then
  echo "Force rebuild requested, skipping hash check"
fi

echo "Building database image with DDL hash: ${DDL_HASH:0:12}"

NETWORK_NAME="build-db-container-network$RANDOM"
DB_CONTAINER_NAME="$SOFTWARE_COMPONENT-test-db$RANDOM"

cleanup() {
  docker stop $DB_CONTAINER_NAME || true
  docker rm $DB_CONTAINER_NAME || true
  docker network rm ${NETWORK_NAME} || true
}

cleanup 2>/dev/null                       # need to cleanup in case of last run failed
docker rmi $IMAGE_TAG 2>/dev/null || true # in order to replace in same commit

docker network create -d bridge ${NETWORK_NAME}

docker pull -q postgres:18
docker run -d --name $DB_CONTAINER_NAME --network=${NETWORK_NAME} \
  -e POSTGRES_USER=$RAMBLER_USER -e POSTGRES_PASSWORD=$RAMBLER_PASSWORD \
  -e POSTGRES_DB=$RAMBLER_DATABASE -e PGDATA=$PGDATA postgres:16

docker pull -q busybox
docker pull -q zhaowde/rambler:latest

while ! docker run --network=${NETWORK_NAME} --rm busybox nc -z ${DB_CONTAINER_NAME} ${RAMBLER_PORT}; do
  sleep 0.1 # wait for 1/10 of the second before check again
done

# setup the default database
docker exec $DB_CONTAINER_NAME psql --username ${RAMBLER_USER} --dbname ${RAMBLER_DATABASE} -c "ALTER USER ${RAMBLER_USER} SET search_path TO ${RAMBLER_SCHEMA}"
docker exec $DB_CONTAINER_NAME psql --username ${RAMBLER_USER} --dbname ${RAMBLER_DATABASE} -c "CREATE SCHEMA ${RAMBLER_SCHEMA}"
docker run -v "${SCRIPTS}:/scripts:ro" --network=${NETWORK_NAME} \
  -e RAMBLER_DRIVER -e RAMBLER_PROTOCOL -e RAMBLER_HOST=${DB_CONTAINER_NAME} \
  -e RAMBLER_PORT -e RAMBLER_USER -e RAMBLER_PASSWORD -e RAMBLER_DATABASE -e \
  RAMBLER_DIRECTORY -e RAMBLER_TABLE -e RAMBLER_SCHEMA --rm zhaowde/rambler:latest

echo $IMAGE_TAG

docker commit --change "LABEL ddl.hash=$DDL_HASH" $DB_CONTAINER_NAME $IMAGE_TAG

cleanup

echo ""
echo "✓ Created image with DDL hash: ${DDL_HASH:0:12}"
if [ -n "$CLONES" ]; then
  echo "Contains $CLONES cloned databases (e.g. $RAMBLER_DATABASE, $RAMBLER_DATABASE, etc.)"
fi
echo $IMAGE_TAG

#!/bin/bash

set -e

export RAMBLER_DRIVER=postgresql
export RAMBLER_PROTOCOL=tcp
export RAMBLER_PORT=5432
export RAMBLER_USER=postgres
export RAMBLER_PASSWORD=password
export RAMBLER_DIRECTORY=/scripts
export RAMBLER_DATABASE=playground
export RAMBLER_TABLE=migrations
export RAMBLER_SCHEMA=playground
export PGDATA=/var/lib/postgresql-static/data
export SCRIPTS=../../infrastructure/roles/basis/files/ddl/scripts

## === PREPARE ===
SOFTWARE_COMPONENT=infrastructure-agents
IMAGE_NAME=alloy-$SOFTWARE_COMPONENT-test-database

NETWORK_NAME="build-db-container-network$RANDOM"
DB_CONTAINER_NAME="alloy-$SOFTWARE_COMPONENT-test-db$RANDOM"
IMAGE_TAG=$IMAGE_NAME:latest
CLONES=$1

cleanup() {
  docker stop $DB_CONTAINER_NAME || true
  docker rm $DB_CONTAINER_NAME || true
  docker network rm ${NETWORK_NAME} || true
}

cleanup 2>/dev/null                       # need to cleanup in case of last run failed
docker rmi $IMAGE_TAG 2>/dev/null || true # in order to replace in same commit

docker network create -d bridge ${NETWORK_NAME}

docker pull -q docker.rcplus.io/postgres:16
docker run -d --name $DB_CONTAINER_NAME --network=${NETWORK_NAME} \
  -e POSTGRES_USER=$RAMBLER_USER -e POSTGRES_PASSWORD=$RAMBLER_PASSWORD \
  -e POSTGRES_DB=$RAMBLER_DATABASE -e PGDATA=$PGDATA postgres:16

docker pull -q docker.rcplus.io/busybox
docker pull -q docker.rcplus.io/zhaowde/rambler:latest

while ! docker run --network=${NETWORK_NAME} --rm busybox nc -z ${DB_CONTAINER_NAME} ${RAMBLER_PORT}; do
  sleep 0.1 # wait for 1/10 of the second before check again
done

# setup the default database
docker exec $DB_CONTAINER_NAME psql --username ${RAMBLER_USER} --dbname ${RAMBLER_DATABASE} -c "ALTER USER ${RAMBLER_USER} SET search_path TO ${RAMBLER_SCHEMA}"
docker exec $DB_CONTAINER_NAME psql --username ${RAMBLER_USER} --dbname ${RAMBLER_DATABASE} -c "CREATE SCHEMA ${RAMBLER_SCHEMA}"
docker run -v "/$(pwd)/${SCRIPTS}:/scripts:ro" --network=${NETWORK_NAME} \
  -e RAMBLER_DRIVER -e RAMBLER_PROTOCOL -e RAMBLER_HOST=${DB_CONTAINER_NAME} \
  -e RAMBLER_PORT -e RAMBLER_USER -e RAMBLER_PASSWORD -e RAMBLER_DATABASE -e \
  RAMBLER_DIRECTORY -e RAMBLER_TABLE -e RAMBLER_SCHEMA --rm docker.rcplus.io/zhaowde/rambler:latest

if [ -z "$CLONES" ]; then
  CLONES=$(getconf _NPROCESSORS_ONLN) # gets number of logical processors available
fi
# loop CLONES times and create playground_n identical databases for parallel testing
for i in $(seq 1 $CLONES); do
  docker exec $DB_CONTAINER_NAME psql --username ${RAMBLER_USER} -c "CREATE DATABASE playground_${i} TEMPLATE playground"
done


echo $IMAGE_TAG

docker commit $DB_CONTAINER_NAME $IMAGE_TAG

cleanup

echo ""
echo "Created image. Publish this to repo or use it locally to start a local Postgres 16 database with an empty schema."
if [ -n "$CLONES" ]; then
  echo "Contains $CLONES cloned databases (e.g. playground_1, playground_2, etc.)"
fi
echo $IMAGE_TAG

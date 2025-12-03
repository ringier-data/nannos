#!/bin/bash

source_folder=$1

set -e

if [[ -z "$source_folder" ]]; then
  echo "Missing required argument 'source_folder'"
  exit 1
fi

if [[ ${SKIP_TESTS} == "1" ]] || [[ ${SKIP_TESTS} == "true" ]]; then
  echo "WARNING: Skipping tests as SKIP_TESTS flag was set to $SKIP_TESTS"
  exit 0
fi

dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

# shellcheck source=.
. "$dir"/ci-safeguard.sh
# shellcheck source=.
. "$dir"/ci-include.sh

#
# There are some node.js apps do not require an "npm install" before running the test, for instance, the "npm install" is happening inside
# a Docker container, while all the logic are encapsulated into the "npm run test". For these cases, set SKIP_INSTALL to save some time.
if [[ ${SKIP_INSTALL} == "1" ]] || [[ ${SKIP_INSTALL} == "true" ]]; then
  SKIP_INSTALL=1
else
  SKIP_INSTALL=0
fi

#
# [NOTE-js] This assumes that we follow the practice where dbt project is directly in the root dbt folder e.g dbt/alloy-project 
if [[ "$source_folder" == *"/dbt/"* ]]; then
  dbt_project_folder=$(basename "$source_folder")
  source_folder=$(dirname "$source_folder")
fi 

echo "Running tests in $source_folder"
pushd "${source_folder}"

if [[ -f "package-lock.json" ]]; then
  echo "Detected node.js project. Will run tests with npm..."
  (( SKIP_INSTALL == 0 )) && npm --no-color ci
  npm --no-color test
elif [[ -f "pyproject.toml" ]]; then
  if [[ -f "uv.lock" ]]; then
    echo "Detected Python project with uv. Will run pytest tests..."
    (( SKIP_INSTALL == 0 )) && uv sync --no-progress

    if [[ "$source_folder" == *"/dbt" ]]; then
      uv run dbt deps --project-dir "$dbt_project_folder" --profiles-dir "$dbt_project_folder"
    fi

    ENV=$ENV uv run pytest
  elif [[ -f "poetry.lock" ]]; then
    echo "Detected Python project with Poetry. Will run pytest tests..."
    poetry config virtualenvs.create true
    poetry config virtualenvs.in-project true
    (( SKIP_INSTALL == 0 )) && poetry install --no-interaction --no-ansi

    if [[ "$source_folder" == *"/dbt" ]]; then
      .venv/bin/dbt deps --project-dir "$dbt_project_folder" --profiles-dir "$dbt_project_folder"
    fi

    ENV=$ENV poetry run pytest
    echo "Cleaning up test environment..."
    rm -rf .venv
  else
    echo "ERROR: Could not determine Python project type. Neither uv.lock nor poetry.lock found."
    exit 1
  fi
else
  echo "ERROR: Could not determine test suite to run."
  exit 1
fi

popd

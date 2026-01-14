#!/bin/bash

source_folder=$1

set -e

if [[ -z "$source_folder" ]]; then
  echo "Missing required argument 'source_folder'"
  exit 1
fi

if [[ $(cat /tmp/is_deploy_flag 2>/dev/null) != "1" ]]; then
  if [[ ${AGGRESSIVE_DEVELOPMENT} == "1" ]]; then
    echo "Skipping deploy as FORCE_DEPLOY is not set and branch isn't develop or main"
  else
    echo "Skipping deploy as deployment flag is not set"
  fi
  exit 0
fi

dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

# shellcheck source=.
. "$dir"/ci-safeguard.sh
# shellcheck source=.
. "$dir"/ci-include.sh

pushd "${source_folder}"

# collect package metadata. NOTE: the package_name is a scoped one.
package_name=$(jq -crM '.name' <package.json)
package_version=$(jq -crM '.version' <package.json)
package_version_remote=$(npm info "$package_name"@"$package_version" version || true)

to_publish=0
if [[ -z "$package_version_remote" ]]; then
  # package does not exist at the remote registry
  echo "Publishing npm package from $source_folder"
  npm --no-color ci
  npm --no-color run build
  to_publish=1
fi

if [[ ${CODEBUILD_WEBHOOK_HEAD_REF} == "refs/heads/main" || ${FORCE_BRANCH} == "main" ]]; then
  if ((to_publish == 0)); then
    echo Package exists, tagging "$package_name"@"$package_version" as "latest"
    npm dist-tag add "$package_name"@"$package_version" latest
  else
    echo Package does not exist, publishing "$package_name"@"$package_version" as "latest"
    npm --no-color publish --tag latest
  fi
elif [[ ${CODEBUILD_WEBHOOK_HEAD_REF} == "refs/heads/develop" || ${FORCE_BRANCH} == "develop" ]]; then
  if ((to_publish == 0)); then
    echo Package exists, tagging "$package_name"@"$package_version" as "next"
    npm dist-tag add "$package_name"@"$package_version" next
  else
    echo Package does not exist, publishing "$package_name"@"$package_version" as "next"
    npm --no-color publish --tag next
  fi
else
  if ((to_publish == 0)); then
    echo Package exists, tagging "$package_name"@"$package_version" as "unstable"
    npm dist-tag add "$package_name"@"$package_version" unstable
  else
    echo Package does not exist, publishing "$package_name"@"$package_version" as "unstable"
    npm --no-color publish --tag unstable
  fi
fi

popd

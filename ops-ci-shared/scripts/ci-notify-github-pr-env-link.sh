#!/bin/bash

set -e

if [[ ${CODEBUILD_BUILD_SUCCEEDING} == "1" ]] && [[ -f /tmp/pr_env_url ]] && [[ $(cat /tmp/pr_number 2>/dev/null) =~ ^[0-9]+$ ]]; then
  PR_NUMBER="$(cat /tmp/pr_number 2>/dev/null)"
  URL=$(cat /tmp/pr_env_url 2>/dev/null)

  export GH_TOKEN=$(aws --region "${AWS_REGION}" ssm get-parameter --output json --name /ops-ci/github-access-token --with-decryption | jq -crM '.Parameter.Value')
  gh pr comment "$PR_NUMBER" -b "The PR environment is ready at ${URL} (${ENV} @ \`${CODEBUILD_RESOLVED_SOURCE_VERSION}\`)" > /dev/null

fi

rm -f /tmp/pr_env_url
rm -f /tmp/pr_number

#!/bin/bash

set -e

dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

# shellcheck source=.
. "$dir"/ci-safeguard.sh

if [[ $(cat /tmp/is_deploy_flag 2>/dev/null) == "1" ]]; then
    noun="Deploy"
    icon=":rocket:"
else
    noun="Build"
    icon=":hammer_and_wrench:"
fi

if [[ ${CODEBUILD_BUILD_SUCCEEDING} == "1" ]]; then
    color="#3fc380"
    adjective="succeeded"
    style="primary"
else
    color="#FF0000"
    adjective="failed"
    icon=":poop:"
    style="danger"
fi

if [[ $1 == "only-deploy-failures" ]]; then
  if [[ ${noun} != 'Deploy' ]] || [[ ${adjective} != "failed" ]]; then
    echo 'Will not notify slack since "only-deploy-failures" is set and the deployment did not fail';
    exit 0;
  fi
fi


# remove the deploy flags to avoid confusing the next deployment
rm -f /tmp/is_deploy_flag
rm -f /tmp/is_pr_env_deploy_flag

# remove the trailing .git
# e.g. https://github.com/alloy-ch/rcplus-ci-debug.git ==> https://github.com/alloy-ch/rcplus-ci-debug
repo_url=${CODEBUILD_SOURCE_REPO_URL//".git"/}
# e.g. https://github.com/alloy-ch/rcplus-ci-debug ==> rcplus-ci-debug
# get the part after the last splash
repo="${repo_url##*/}"
build_url="${CODEBUILD_BUILD_URL}"
# get the branch name
# e.g. refs/heads/develop ==> develop, or, refs/heads/feature/pr-to-develop ==> feature/pr-to-develop
branch=${CODEBUILD_WEBHOOK_HEAD_REF//refs\/heads\//}
if [[ -z "$branch" ]]; then
    branch="${CODEBUILD_SOURCE_VERSION}"
fi

slack_body=$(cat << EOF
{
  "text": "${icon} *${repo}* (${PROJECT_ID}-${ENV})",
  "attachments": [
    {
      "color": "${color}",
      "blocks": [
        {
          "type": "section",
          "text": {
            "type": "mrkdwn",
            "text": " *${noun} ${adjective}* for source \`${branch}\`"
          }
        },
        {
          "type": "actions",
          "elements": [
            {
              "type": "button",
              "text": {
                "type": "plain_text",
                "text": "Open in CodeBuild"
              },
              "url": "${build_url}",
              "style": "${style}"
            }
          ]
        }
      ]
    }
  ]
}
EOF
)

slack_webhook=$(aws --region "${AWS_REGION}" ssm get-parameter --output json --name "/ops-ci/slack-webhook-notifications" --with-decryption | jq -crM '.Parameter.Value')
# shellcheck disable=SC1083
response=$(curl --write-out %{http_code} --silent --output /dev/null --request POST --header 'Content-Type: application/json' --data "$slack_body" "${slack_webhook}")

if [[ "${response}" != "200" ]]; then
    echo "Bad status code from Slack: ${response}"
    exit 1
else
    echo "Notification sent to Slack"
fi

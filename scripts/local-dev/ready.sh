#!/usr/bin/env bash
# Readiness probe of process-compose.yaml: succeeds once URL answers 2xx, and from then on without
# asking again. process-compose keeps running a readiness probe for as long as the process runs;
# an HTTP probe every two seconds fills the services' access logs (and an authenticated route's
# warnings) for the whole session. Readiness is what the stack waits for at start; a service that
# dies afterwards shows as its process exiting. start-local.sh clears the marks at each start.
#
#   ready.sh <stack-dir> <name> <url>

mark="$1/ready/$2"
[[ -f "$mark" ]] && exit 0
curl -fsS -o /dev/null --max-time 3 "$3" 2>/dev/null || exit 1
mkdir -p "$1/ready" && touch "$mark"

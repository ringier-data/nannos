#!/bin/bash

# Load ~/.npmrc into env var
NPMRC_ENCODED=$(cat ~/.npmrc | base64 | tr -d \\n)
export NPMRC_ENCODED

# wrap pushd and popd to swallow the stdout messages
function pushd() {
  command pushd "$@" >/dev/null
}


function popd() {
  command popd "$@" >/dev/null
}

#!/usr/bin/env bash
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
set -euo pipefail

if (( $# < 2 || $# > 3 )); then
    echo "Usage: $0 <model> <application> [approved-legacy-uids]" >&2
    exit 2
fi
model=$1
app=$2
legacy_uids=${3:-}

run_action() {
    local output
    if ! output=$(juju run -m "$model" --wait 2m --format json "$app/leader" "$@"); then
        printf '%s\n' "$output" >&2
        return 1
    fi
    jq -er '
        if length == 1 then .[] else error("Expected one action result") end
        | if .status == "completed" and .results.state then .results.state else error(.) end
    ' <<< "$output"
}

[[ $(run_action prepare-uninstall) == prepared ]]
deadline=$((SECONDS + 600))
while (( SECONDS < deadline )); do
    state=$(run_action uninstall "legacy-uids=$legacy_uids")
    case $state in
        removed) exec juju remove-application -m "$model" "$app" ;;
        waiting) sleep 5 ;;
        *) echo "Unexpected uninstall state: $state" >&2; exit 1 ;;
    esac
done
echo "Uninstall did not finish. The application and relations were left in place." >&2
exit 1

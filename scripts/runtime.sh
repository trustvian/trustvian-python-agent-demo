#!/usr/bin/env bash
#
# runtime.sh — the local Trustvian control plane, for callers that drive many
# evaluation runs against one of them.
#
#     scripts/runtime.sh up      start one, print its endpoint
#     scripts/runtime.sh url     print the endpoint of the one already running
#     scripts/runtime.sh down    stop it, if this directory started it
#
# This is **not** a stand-in for anything. `trustvian dev` starts a control
# plane of its own when no --api-url is given, and stops it on the way out —
# which is right for one run and wrong for several, because two runs that need
# comparing must share one database, and a browser looking at the result needs
# the server to outlive the run that produced it.
#
# Trustvian's own docs/local-development.md § "Running the parts separately"
# describes exactly this shape: a control plane that outlives several runs, with
# each run attaching to it via --api-url. That is also task 078's CI path.
#
# The lifecycle itself lives in scripts/lib.sh, where demo.sh and smoke.sh
# already use it. This file is only the dispatch, so there is one
# implementation of "start a control plane and prove it answers".

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() {
    cat >&2 <<'USAGE'
usage: runtime.sh up|url|down

  up     start a control plane that outlives this command, print its endpoint
  url    print the endpoint published for this directory
  down   stop it — only ever one serving this directory's .trustvian/
USAGE
    exit 2
}

runtime_pid_file() { printf '%s\n' "$RUNTIME_DIR/trustvian-local.pid"; }

runtime_up() {
    # RUNTIME_DETACH tells start_runtime to write a pid file instead of tracking
    # the process for the EXIT trap: this command's whole purpose is a runtime
    # that survives it.
    RUNTIME_DETACH="yes"
    start_runtime
    printf '%s\n' "$API_URL"
}

runtime_url() {
    local discovery="$STATE_DIR/runtime.json"
    [ -s "$discovery" ] || fail "no Trustvian runtime is recorded at $discovery.
       Start one with: scripts/runtime.sh up"
    jq -r '.api_url // empty' "$discovery"
}

# runtime_down stops only a runtime serving *this* directory's state.
#
# Matched on the state directory rather than on the pid alone: a pid file can
# outlive its process and the number can be recycled, so the command line is
# checked before anything is signalled. A trustvian-local someone started by
# hand for another project has a different --state-dir and is never a
# candidate — and neither is one `trustvian dev` started, which keeps its state
# under ~/.trustvian/dev/.
runtime_down() {
    local pid_file; pid_file="$(runtime_pid_file)"
    if [ ! -s "$pid_file" ]; then
        log "no detached runtime recorded; nothing to stop"
        return 0
    fi
    local pid; pid="$(cat "$pid_file")"
    rm -f "$pid_file"
    case "$pid" in ''|*[!0-9]*) log "ignoring unusable pid '$pid'"; return 0 ;; esac
    if ! ps -o command= -p "$pid" 2>/dev/null | grep -q -- "--state-dir $STATE_DIR"; then
        log "pid $pid is not a runtime serving $STATE_DIR; leaving it alone"
        return 0
    fi
    kill "$pid" 2>/dev/null || true
    local deadline=$(( SECONDS + 15 ))
    while (( SECONDS < deadline )); do
        kill -0 "$pid" 2>/dev/null || { log "runtime stopped"; return 0; }
        sleep 0.1
    done
    fail "the Trustvian runtime at pid $pid did not stop within 15s"
}

demo_init
case "${1:-}" in
    up)   runtime_up ;;
    url)  runtime_url ;;
    down) runtime_down ;;
    *)    usage ;;
esac

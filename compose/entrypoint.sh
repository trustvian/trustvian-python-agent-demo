#!/usr/bin/env bash
#
# Entrypoint of the `demo` service. Each subcommand is one of the
# repository's own entry points, run unchanged inside the container:
#
#   smoke                 make smoke — every guarantee, no model needed
#   scenario [FILE]       tools/scenario.py (default scenarios/support-demo.yaml);
#                         needs Ollama at ollama.localhost:11434
#   stability [RUNS]      tools/stability.py scenarios/stability.yaml --runs RUNS
#   web                   serve this container's control-plane state (after a
#                         scenario in the SAME container, e.g. from `shell`)
#                         on http://127.0.0.1:8080/ (run with --service-ports);
#                         TRUSTVIAN_WEB=1 makes `scenario` do this by itself
#   make TARGET...        any Makefile target
#   shell                 bash in the container
#
# Results go to /out (bind-mounted to ./out by compose.yaml); the logs under
# .runtime/ are copied there when the command ends.
set -euo pipefail

cd "${DEMO_ROOT:-/workspace/trustvian-python-agent-demo}"
OUT="${DEMO_OUT:-/out}"
mkdir -p "$OUT"

usage() {
    cat <<'USAGE'
usage: docker compose run --rm [--service-ports] demo <subcommand>

  smoke                 make smoke — every guarantee, no model needed
  scenario [FILE]       tools/scenario.py (default scenarios/support-demo.yaml);
                        needs Ollama at ollama.localhost:11434
  stability [RUNS]      tools/stability.py scenarios/stability.yaml --runs RUNS
  web                   serve this container's control-plane state on
                        http://127.0.0.1:8080/ (TRUSTVIAN_WEB=1 makes
                        `scenario` do this by itself)
  make TARGET...        any Makefile target
  shell                 bash in the container
USAGE
    exit 2
}

copy_logs() {
    if [ -d .runtime ]; then
        mkdir -p "$OUT/runtime"
        cp -f .runtime/*.log "$OUT/runtime/" 2>/dev/null || true
    fi
}
trap copy_logs EXIT

# serve_web keeps the control plane of the LAST scenario up for a browser.
#
# tools/scenario.py starts a runtime through scripts/runtime.sh and stops it
# when the comparison is done, which is right for CI and leaves nothing to
# look at. The database it wrote is still in .trustvian/, so a fresh
# trustvian-local on the same state directory serves exactly that result.
# Loopback only, by trustvian-local's own rule; socat relays it to the
# container's 0.0.0.0:8080, which `docker compose run --service-ports`
# publishes on the host's 127.0.0.1.
serve_web() {
    local port="${TRUSTVIAN_PUBLIC_PORT:-8080}"
    if [ ! -d .trustvian ]; then
        echo "web: no control-plane state yet — run 'scenario' first" >&2
        exit 2
    fi
    echo
    echo "web: serving the last scenario's control plane"
    echo "web: open http://127.0.0.1:${port}/ on the host  (Ctrl-C to stop)"
    .demo/bin/trustvian-local --state-dir "$PWD/.trustvian" --listen 127.0.0.1:18080 \
        >"$OUT/runtime-web.log" 2>&1 &
    local local_pid=$!
    socat "TCP-LISTEN:${port},fork,reuseaddr" "TCP:127.0.0.1:18080" &
    local socat_pid=$!
    trap 'kill -TERM "$socat_pid" "$local_pid" 2>/dev/null || true' TERM INT
    wait "$local_pid" || true
    kill -TERM "$socat_pid" 2>/dev/null || true
}

stream_flag() {
    if [ "${AGENT_STREAM:-yes}" = "yes" ]; then printf -- '--stream'; fi
}

case "${1:-help}" in
    smoke)
        make smoke
        ;;
    scenario)
        shift
        file="${1:-scenarios/support-demo.yaml}"
        status=0
        # shellcheck disable=SC2046  # stream_flag is empty or one flag
        .demo/tools-venv/bin/python tools/scenario.py "$file" \
            --model "${OLLAMA_MODEL:-gemma3:4b}" \
            --results "$OUT/scenario-results.json" $(stream_flag) || status=$?
        echo
        echo "results: ./out/scenario-results.json  (exit $status; 1 means the gate said FAIL)"
        if [ "${TRUSTVIAN_WEB:-0}" = "1" ]; then
            serve_web
        fi
        exit "$status"
        ;;
    stability)
        shift
        .demo/tools-venv/bin/python tools/stability.py scenarios/stability.yaml \
            --runs "${1:-10}" --temperature "${TEMPERATURE:-0.7}" \
            --model "${OLLAMA_MODEL:-gemma3:4b}" \
            --results "$OUT/stability-results.json"
        echo "results: ./out/stability-results.json"
        ;;
    web)
        serve_web
        ;;
    make)
        shift
        make "$@"
        ;;
    shell)
        exec bash
        ;;
    help|-h|--help)
        usage
        ;;
    *)
        exec "$@"
        ;;
esac

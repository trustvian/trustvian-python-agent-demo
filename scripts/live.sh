#!/usr/bin/env bash
#
# Live mode: three agents that keep working until Ctrl-C. See live.py.
#
# A separate entry point. It shares lib.sh's helpers and nothing else: the
# demo, smoke, scenario, stability and fidelity paths do not call it and are
# not changed by it. Its mock services are its own instance, started with
# CRM_ERROR_RATE (default 0.05) so the fleet shows the occasional failed call;
# every other path starts them with no error rate, deterministic as before.
#
# Native: dev starts its own control plane and prints its Web address. With
# LIVE_API_URL set (the Compose `live` service sets it), dev attaches to that
# control plane instead.

set -euo pipefail

# Live mode's model is qwen2.5:3b, not the demo's gemma3:4b. Measured with three
# workers sharing one Ollama: gemma3:4b fills every field of the action schema
# with prose and took 100–290 s a ticket; qwen2.5:3b 10–14 s at the median.
# OLLAMA_MODEL still overrides it. Set before lib.sh, which reads it.
export OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:3b}"

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

demo_init

"$DEMO_ROOT/scripts/bootstrap.sh"
require_trustvian_dev

# The host's Ollama is started or reused as `make demo` does; in a container,
# where there is no ollama binary, the model must already answer.
if command -v ollama >/dev/null 2>&1; then
    ensure_ollama
else
    require_model_answers
fi

export CRM_ERROR_RATE="${CRM_ERROR_RATE:-0.05}"
start_mocks
log "local services ready on port $MOCK_PORT; CRM error rate $CRM_ERROR_RATE"

attach=()
if [ -n "${LIVE_API_URL:-}" ]; then
    attach=(--api-url "$LIVE_API_URL")
fi

# The developer's Ctrl-C reaches dev from the terminal by itself, and dev then
# stops live.py and completes the run. `docker compose stop` sends SIGTERM to
# this script only, so that one is passed on.
SUPPORT_AGENT_PORT="$MOCK_PORT" \
OLLAMA_MODEL="$OLLAMA_MODEL_NAME" \
PYTHONPATH="${PYTHONPATH:-$DEMO_ROOT}" \
    "$BIN_DIR/trustvian" dev ${attach[@]+"${attach[@]}"} \
        --project "$PROJECT_ID" \
        --agent "${TRUSTVIAN_LIVE_AGENT:-support-fleet}" \
        --environment "$ENVIRONMENT" \
        --candidate live \
        --instrumentation existing \
        -- "$VENV_DIR/bin/python" "$DEMO_ROOT/live.py" &
DEV_PID=$!
trap ':' INT
trap 'kill -TERM "$DEV_PID" 2>/dev/null || true' TERM

status=0
wait "$DEV_PID" || status=$?
while kill -0 "$DEV_PID" 2>/dev/null; do
    status=0
    wait "$DEV_PID" || status=$?
done
exit "$status"

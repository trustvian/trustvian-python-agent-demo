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
# Every agent is its own Trustvian run: live.py starts one `trustvian dev` per
# role, each attached to the control plane at TRUSTVIAN_API_URL, which must
# already be running (natively in a terminal of its own; the Compose `live`
# service starts one in its container).

set -euo pipefail

if [ -z "${TRUSTVIAN_API_URL:-}" ]; then
    echo "live: set TRUSTVIAN_API_URL to a running control plane; start one with \`.demo/bin/trustvian-local --state-dir .demo/live --listen 127.0.0.1:8080\`" >&2
    exit 2
fi

# Live mode's model is qwen2.5:3b, not the demo's gemma3:4b. Measured with three
# workers sharing one Ollama: gemma3:4b fills every field of the action schema
# with prose and took 100–290 s a ticket; qwen2.5:3b 10–14 s at the median.
# OLLAMA_MODEL still overrides it. Set before lib.sh, which reads it.
export OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:3b}"

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

demo_init

"$DEMO_ROOT/scripts/bootstrap.sh"
require_trustvian_dev

# The host's Ollama is started or reused as `make demo` does. In a container
# there is no ollama binary, and the model must already answer. That is asked
# with the agent's own HTTP client rather than lib.sh's curl probe: curl
# resolves every *.localhost name to its own loopback (RFC 6761) and ignores
# the extra_hosts entry that points ollama.localhost at the host.
if command -v ollama >/dev/null 2>&1; then
    ensure_ollama
else
    "$VENV_DIR/bin/python" - "$OLLAMA_AGENT_API" "$OLLAMA_MODEL_NAME" <<'PY' \
        || fail "the model $OLLAMA_MODEL_NAME did not answer at $OLLAMA_AGENT_API"
import sys, requests
reply = requests.post(sys.argv[1] + "/api/chat", timeout=180, json={
    "model": sys.argv[2], "stream": False,
    "messages": [{"role": "user", "content": "Reply with: ok"}],
    "options": {"temperature": 0, "num_predict": 4}})
reply.raise_for_status()
reply.json()["message"]["content"]  # a usable reply; never printed
PY
    log "$OLLAMA_MODEL_NAME answered at $OLLAMA_AGENT_API"
fi

export CRM_ERROR_RATE="${CRM_ERROR_RATE:-0.05}"
start_mocks
log "local services ready on port $MOCK_PORT; CRM error rate $CRM_ERROR_RATE"

# live.py finds this build's `trustvian` on PATH and passes the project to
# every dev it starts. The developer's Ctrl-C reaches live.py from the
# terminal by itself, and live.py then stops every dev, each completing its
# run. `docker compose stop` sends SIGTERM to this script only, so that one
# is passed on.
PATH="$BIN_DIR:$PATH" \
TRUSTVIAN_PROJECT="$PROJECT_ID" \
SUPPORT_AGENT_PORT="$MOCK_PORT" \
OLLAMA_MODEL="$OLLAMA_MODEL_NAME" \
PYTHONPATH="${PYTHONPATH:-$DEMO_ROOT}" \
    "$VENV_DIR/bin/python" "$DEMO_ROOT/live.py" &
LIVE_PID=$!
trap ':' INT
trap 'kill -TERM "$LIVE_PID" 2>/dev/null || true' TERM

status=0
wait "$LIVE_PID" || status=$?
while kill -0 "$LIVE_PID" 2>/dev/null; do
    status=0
    wait "$LIVE_PID" || status=$?
done
exit "$status"

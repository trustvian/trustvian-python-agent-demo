#!/usr/bin/env bash
#
# model-change.sh — scenarios/model-change.yaml through `trustvian eval run`:
# the support agent on llama3.2 against the same agent on gemma3:4b.
#
# A separate entry point, like live.sh. It shares lib.sh's helpers and nothing
# else: the demo, smoke, scenario, stability and fidelity paths do not call it
# and are not changed by it.
#
# It needs both models (`ollama pull llama3.2 gemma3:4b`). ensure_ollama pulls
# gemma3:4b, the demo's own model, when it is missing, as `make demo` does; a
# missing llama3.2 is named rather than pulled. It starts a fresh control plane
# and the mock services, runs the scenario against them, writes the
# execution's report to .runtime/, and leaves the control plane up so its WebUI
# can be read. `scripts/runtime.sh down` stops it.

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

SCENARIO="scenarios/model-change.yaml"
MODELS=(llama3.2 gemma3:4b)

demo_init

"$DEMO_ROOT/scripts/bootstrap.sh"
require_trustvian_dev

# The server is started or reused as `make demo` does it; gemma3:4b, the
# candidate's model, is the one ensure_ollama asks for an answer.
OLLAMA_MODEL_NAME="gemma3:4b"
ensure_ollama
for model in "${MODELS[@]}"; do
    ollama list 2>/dev/null | awk '{print $1}' | grep -qxF -e "$model" -e "$model:latest" \
        || fail "$model is not installed. This scenario needs both models:

           ollama pull ${MODELS[*]}"
done
log "${MODELS[*]} installed"

# Detached, as scripts/runtime.sh up does it: the reader opens Change Impact on
# this control plane after the scenario ends.
RUNTIME_DETACH="yes"
start_runtime
start_mocks
log "control plane $API_URL; local services on port $MOCK_PORT"

# The scenario's sides inherit this environment: the mock services' port, which
# start_mocks chose, and the import path dev_run sets for every other run.
export SUPPORT_AGENT_PORT="$MOCK_PORT"
export PYTHONPATH="${PYTHONPATH:-$DEMO_ROOT}"

log "running $SCENARIO (both sides, one repetition at a time)"
set +e
"$BIN_DIR/trustvian" eval run --api-url "$API_URL" --scenario "$SCENARIO" \
    2>"$RUNTIME_DIR/model-change.log" | tee "$RUNTIME_DIR/model-change.txt"
status=${PIPESTATUS[0]}
set -e

execution="$(sed -n '1s/.*(execution \([^)]*\)).*/\1/p' "$RUNTIME_DIR/model-change.txt")"
if [ -z "$execution" ]; then
    fail "eval run exited $status without starting an execution:
$(tail -30 "$RUNTIME_DIR/model-change.log")"
fi
if [ "$status" -ne 0 ] && [ "$status" -ne 1 ]; then
    fail "eval run exited $status: execution $execution did not complete, so it has
       no verdict and no report. Its repetitions' output:
$(tail -30 "$RUNTIME_DIR/model-change.log")"
fi

"$BIN_DIR/trustvian" eval report "$execution" --api-url "$API_URL" --format md \
    >"$RUNTIME_DIR/model-change.md"
"$BIN_DIR/trustvian" eval report "$execution" --api-url "$API_URL" --json \
    >"$RUNTIME_DIR/model-change.json"

cat <<DONE

  execution  $execution
  report     .runtime/model-change.md (Markdown), .runtime/model-change.json
  WebUI      $API_URL/#execution=$execution   (Change Impact)

  The control plane keeps running for the WebUI; stop it with
  scripts/runtime.sh down.
DONE

# eval run's own exit: 1 is the gate's FAIL, which a model change produces by
# itself (the README says why), not a broken run.
exit "$status"

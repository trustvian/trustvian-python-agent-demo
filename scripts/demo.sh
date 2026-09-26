#!/usr/bin/env bash
#
# The whole demo. One command, no arguments, nothing to configure.

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

demo_init

echo
echo "Trustvian Local Agent Demo"
echo "--------------------------"
echo

"$DEMO_ROOT/scripts/bootstrap.sh"

echo
echo "LLM:   Ollama"
echo "Model: $OLLAMA_MODEL_NAME"
echo
ensure_ollama

start_runtime
log "control plane ready"

start_mocks
log "local services ready on port $MOCK_PORT (crm/knowledge/mail/export .localhost)"

# The hierarchy exists before any telemetry does. Zero-input is a statement
# about what the developer types into the browser, never about the platform
# inventing entities: ingest into a run that does not exist, or one that is not
# running, is refused — and should be.
# One evaluation run is one invocation of scripts/tv-dev.sh, the stand-in for
# Trustvian task 077's `trustvian dev`. It composes the Collector, the
# OpenTelemetry environment and the run lifecycle around a command it does not
# modify — here, the model-driven agent.
#
# How much evidence to wait for is read from the agent's own run summary
# rather than computed: the model decides how many turns to take, so the
# number cannot be known in advance.
tv_dev() {
    local run_id="$1" candidate="$2" profile="$3" mode="$4"
    SUPPORT_AGENT_PORT="$MOCK_PORT" \
    SUPPORT_AGENT_MODE="$mode" \
    SUPPORT_AGENT_ROUNDS="$ROUNDS" \
    OLLAMA_MODEL="$OLLAMA_MODEL_NAME" \
        "$DEMO_ROOT/scripts/tv-dev.sh" \
            --api-url "$API_URL" \
            --run-id "$run_id" \
            --candidate "$candidate" \
            --behavioral-profile "$profile" \
            --summary-file "$RUNTIME_DIR/agent-$mode-summary.json" \
            --expect-records-from "$RUNTIME_DIR/agent-$mode-summary.json" \
            --wait-timeout 120 \
            --stream \
            -- "$VENV_DIR/bin/python" "$DEMO_ROOT/agent/main.py"
}

# The hierarchy exists before any telemetry does, and before the browser is
# opened. Zero-input is a statement about what the developer types, never
# about the platform inventing entities: ingest into a run that does not
# exist, or one that is not running, is refused — and should be.
"$DEMO_ROOT/scripts/tv-dev.sh" hierarchy --api-url "$API_URL" \
    --candidate "$REFERENCE_CANDIDATE" >/dev/null
"$DEMO_ROOT/scripts/tv-dev.sh" hierarchy --api-url "$API_URL" \
    --candidate "$CANDIDATE_CANDIDATE" >/dev/null
log "project $PROJECT_ID, agent $AGENT_ID, candidates $REFERENCE_CANDIDATE and $CANDIDATE_CANDIDATE"

echo
echo "─────────────────────────────────────────────"
echo
echo "Open the Trustvian Live view:"
echo
echo "    $API_URL/"
echo

# Whether the developer has to type identifiers is a property of the server,
# so it is asked of the server rather than assumed from a version or a branch.
if live_view_available; then
    LIVE_VIEW="yes"
    echo "  No IDs need to be entered — the Live view discovers the active"
    echo "  agent and run by itself."
else
    LIVE_VIEW="no"
    cat <<'NOTE'
  This Trustvian build does not serve the zero-input Live view's collection
  routes. The demo below is unaffected — the agent, the telemetry and the
  comparison are all real — but the browser cannot discover the run on its
  own, so navigate by ID instead:
      Open tab -> Open by ID -> Evaluation run ID
NOTE
    echo "      $REFERENCE_RUN   (then $CANDIDATE_RUN for the second run)"
fi

pause "Press ENTER when the browser is open..."

echo "REFERENCE"
echo
tv_dev "$REFERENCE_RUN" "$REFERENCE_CANDIDATE" "$REFERENCE_PROFILE" "reference"
REFERENCE_STEPS="$(agent_steps "$RUNTIME_DIR/agent-reference-summary.json")"
echo
log "reference run completed: $(record_count "$REFERENCE_RUN") observations, $(distinct_behaviors "$REFERENCE_RUN") behaviors"

cat <<NOTE

Reference run complete.

The candidate has access to one more tool: it may export a customer record,
reaching export.localhost. Its instructions require the export as part of the
workflow, but not where in the sequence it happens — $OLLAMA_MODEL_NAME decides that.
NOTE
pause "Press ENTER to run the candidate..."

echo "CANDIDATE"
echo
tv_dev "$CANDIDATE_RUN" "$CANDIDATE_CANDIDATE" "$CANDIDATE_PROFILE" "candidate"
CANDIDATE_STEPS="$(agent_steps "$RUNTIME_DIR/agent-candidate-summary.json")"
echo
log "candidate run completed: $(record_count "$CANDIDATE_RUN") observations, $(distinct_behaviors "$CANDIDATE_RUN") behaviors"

echo
echo "Comparing"
compare_runs

# Every number below is read from the control plane's own response. Nothing
# here is hard-coded, and nothing is recomputed: the verdict is the server's.
ADDED="$(jq -r '.behavior_diff.added_count' "$COMPARISON_FILE")"
VERDICT="$(jq -r '.gate.verdict' "$COMPARISON_FILE")"
LIMIT="$(jq -r '.gate.added_behaviors.maximum' "$COMPARISON_FILE")"
ACTUAL="$(jq -r '.gate.added_behaviors.actual' "$COMPARISON_FILE")"

# The five independent gate checks, each read from the server's own verdict
# rather than inferred. `eval compare`'s gate can fail on any one of these,
# and the explanatory note below must say only what actually happened.
REF_EVIDENCE_PASSED="$(jq -r '.gate.reference_evidence.passed' "$COMPARISON_FILE")"
CAND_EVIDENCE_PASSED="$(jq -r '.gate.candidate_evidence.passed' "$COMPARISON_FILE")"
ADDED_PASSED="$(jq -r '.gate.added_behaviors.passed' "$COMPARISON_FILE")"
BLOCK_PASSED="$(jq -r '.gate.block_decisions.passed' "$COMPARISON_FILE")"
CRITICAL_PASSED="$(jq -r '.gate.critical_risk_observations.passed' "$COMPARISON_FILE")"

# The behavioral targets each run observed, straight from the diff.
#
# REFERENCE observed shared + removed (a "removed" behaviour is one the
# reference showed and the candidate did not — it belongs to the reference's
# own list, not the candidate's).
#
# CANDIDATE observed shared + added. Reusing the reference's helper here
# would print a removed behaviour under CANDIDATE as "observed", which is
# the opposite of what `removed` means. Today the candidate is in practice a
# superset of the reference, so removed_count is 0 — but with a model
# choosing the actions that is an observation about this run rather than an
# invariant, which is exactly why the two helpers stay separate.
reference_targets() {
    jq -r '.behavior_diff.deltas[]
           | select(.change == "shared" or .change == "removed")
           | .behavior.target_name' "$COMPARISON_FILE" | sort -u
}
shared_targets() {
    jq -r '.behavior_diff.deltas[]
           | select(.change == "shared")
           | .behavior.target_name' "$COMPARISON_FILE" | sort -u
}
added_targets() {
    jq -r '.behavior_diff.deltas[]
           | select(.change == "added")
           | .behavior.target_name' "$COMPARISON_FILE" | sort -u
}

echo
echo "─────────────────────────────────────────────"
echo
echo "Trustvian"
echo "  Web: $API_URL/"
echo
echo "Model"
echo "  provider: Ollama"
echo "  model:    $OLLAMA_MODEL_NAME"
echo
echo "REFERENCE"
echo "  the model chose:"
while read -r step; do
    [ -n "$step" ] && printf '    %s\n' "$step"
done <<STEPS
$REFERENCE_STEPS
STEPS
echo "  Trustvian observed $(record_count "$REFERENCE_RUN") calls across $(distinct_behaviors "$REFERENCE_RUN") behaviors:"
while read -r target; do
    [ -n "$target" ] && printf '    %-22s observed\n' "$target"
done < <(reference_targets)
echo
echo "CANDIDATE"
echo "  the model chose:"
while read -r step; do
    [ -n "$step" ] && printf '    %s\n' "$step"
done <<STEPS
$CANDIDATE_STEPS
STEPS
echo "  Trustvian observed $(record_count "$CANDIDATE_RUN") calls across $(distinct_behaviors "$CANDIDATE_RUN") behaviors:"
while read -r target; do
    [ -n "$target" ] && printf '    %-22s observed\n' "$target"
done < <(shared_targets)
while read -r target; do
    [ -n "$target" ] && printf '    %-22s NEW\n' "$target"
done < <(added_targets)
echo
echo "BEHAVIORAL DIFF"
echo "  Shared:  $(jq -r '.behavior_diff.shared_count'  "$COMPARISON_FILE")"
echo "  Removed: $(jq -r '.behavior_diff.removed_count' "$COMPARISON_FILE")"
echo "  Added:   $(jq -r '.behavior_diff.added_count'   "$COMPARISON_FILE")"
while read -r target; do
    [ -n "$target" ] && printf '    + %s\n' "$target"
done < <(added_targets)

# The demo's claim is that the model chose the export, not that Python called
# it. If it did not, say so plainly and show what it chose instead: a useful
# failure is worth far more than a demo that quietly stages its own result.
if ! grep -qx "export.localhost" < <(added_targets); then
    echo
    echo "  NOTE: the candidate run did not produce the expected export behavior."
    echo "  $OLLAMA_MODEL_NAME chose: $(printf '%s ' $CANDIDATE_STEPS)"
    echo "  Nothing was injected to force it. Re-running usually resolves a"
    echo "  one-off; a persistent failure means the model is not following the"
    echo "  candidate policy, and this demo reports that rather than hide it."
fi

echo
echo "COMPARISON"
echo "  Added behaviors: $ADDED"
echo "  Gate: $(echo "$VERDICT" | tr '[:lower:]' '[:upper:]')"
echo "  Limit: max-added-behaviors = $LIMIT (actual $ACTUAL)"
echo "  compare exit code: $COMPARE_STATUS"
echo

# eval compare's gate runs five independent checks, and any one of them
# failing produces the same verdict "fail" and the same exit code 1. The
# reassuring, specific story below ("the candidate merely added a behavior")
# is only true when the added-behaviors check is the sole failure — so it is
# only printed then. Any other failing combination is named plainly instead,
# rather than assumed to be this demo's expected story.
if [ "$COMPARE_STATUS" -eq 1 ] && [ "$VERDICT" = "fail" ] \
   && [ "$ADDED_PASSED" = "false" ] \
   && [ "$REF_EVIDENCE_PASSED" = "true" ] \
   && [ "$CAND_EVIDENCE_PASSED" = "true" ] \
   && [ "$BLOCK_PASSED" = "true" ] \
   && [ "$CRITICAL_PASSED" = "true" ]; then
    cat <<NOTE
  This FAIL is the expected outcome. The candidate introduced $ADDED added
  behavior(s) the reference never showed, and the limit supplied was zero.
  It is a deterministic result under the limits chosen for this demo — not a
  finding that the candidate is unsafe, malicious or compromised.
NOTE
elif [ "$COMPARE_STATUS" -eq 1 ] && [ "$VERDICT" = "fail" ]; then
    echo "  Gate FAIL, but not solely from the added-behaviors check:"
    # `if` rather than a bare `[ ... ] && echo ...`: both forms are safe
    # here under `set -e` (a `[ ... ] &&` list is not the last command in
    # its AND-OR list, so errexit does not fire when the test is false —
    # the same form is used safely elsewhere in this file). `if` is used
    # for readability across five checks in a row.
    if [ "$REF_EVIDENCE_PASSED" = "false" ]; then echo "    - reference evidence check failed"; fi
    if [ "$CAND_EVIDENCE_PASSED" = "false" ]; then echo "    - candidate evidence check failed"; fi
    if [ "$ADDED_PASSED" = "false" ]; then echo "    - added-behaviors check failed"; fi
    if [ "$BLOCK_PASSED" = "false" ]; then echo "    - block-decisions check failed"; fi
    if [ "$CRITICAL_PASSED" = "false" ]; then echo "    - critical-risk-observations check failed"; fi
    echo "  This is not a finding that the candidate is unsafe, malicious or compromised."
else
    echo "  Unexpected: this demo expects a gate FAIL with exit 1."
fi

echo
echo "Trustvian remains running for inspection:"
echo
echo "    $API_URL/"
echo
if [ "$LIVE_VIEW" = "no" ]; then
    echo "  Navigate by ID: Open tab -> Open by ID -> Evaluation run ID"
    echo "    $REFERENCE_RUN   $CANDIDATE_RUN"
    echo
fi
# Printed as diagnostics, not as something anyone should have to type into a
# browser. The identifiers exist; discovering them is the product's job.
log "run ids: $REFERENCE_RUN, $CANDIDATE_RUN"
echo
echo "Press Ctrl-C to stop the demo."
echo

# The runtime stays up so the WebUI is usable. Block on the runtime process
# specifically — a bare `wait` returns 0 immediately once the shell has no
# children left to wait for (verified on bash 3.2.57), which would fall
# through here immediately instead of holding the demo open. `wait
# "$RUNTIME_PID"` blocks until that process exits (normally, or via the
# signal that fires the trap below), and stays interruptible by Ctrl-C
# throughout.
wait "$RUNTIME_PID" || true
echo
echo "Trustvian runtime exited."

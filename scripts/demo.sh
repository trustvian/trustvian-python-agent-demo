#!/usr/bin/env bash
#
# The whole demo. One command, no arguments, nothing to configure.

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

demo_init

echo
echo "Trustvian Python Agent Demo"
echo

"$DEMO_ROOT/scripts/bootstrap.sh"

echo "Starting Trustvian"
start_runtime
log "API: $API_URL"
log "Web: $API_URL/"

echo "Starting mock services"
start_mocks
log "crm/knowledge/mail/export .localhost on port $MOCK_PORT"

echo "Creating control-plane objects"
create_control_plane
log "project $PROJECT_ID, agent $AGENT_ID, candidates $REFERENCE_CANDIDATE and $CANDIDATE_CANDIDATE"

echo
echo "REFERENCE  ($REFERENCE_RUN)"
run_evaluation "$REFERENCE_RUN" "$REFERENCE_CANDIDATE" "$REFERENCE_PROFILE" \
               "reference" "$REFERENCE_ACTIONS"
log "records: $(record_count "$REFERENCE_RUN")   distinct behaviors: $(distinct_behaviors "$REFERENCE_RUN")"

echo
echo "CANDIDATE  ($CANDIDATE_RUN)"
run_evaluation "$CANDIDATE_RUN" "$CANDIDATE_CANDIDATE" "$CANDIDATE_PROFILE" \
               "candidate" "$CANDIDATE_ACTIONS"
log "records: $(record_count "$CANDIDATE_RUN")   distinct behaviors: $(distinct_behaviors "$CANDIDATE_RUN")"

echo
echo "Comparing"
compare_runs

# Every number below is read from the control plane's own response. Nothing
# here is hard-coded, and nothing is recomputed: the verdict is the server's.
ADDED="$(jq -r '.behavior_diff.added_count' "$COMPARISON_FILE")"
VERDICT="$(jq -r '.gate.verdict' "$COMPARISON_FILE")"
LIMIT="$(jq -r '.gate.added_behaviors.maximum' "$COMPARISON_FILE")"
ACTUAL="$(jq -r '.gate.added_behaviors.actual' "$COMPARISON_FILE")"

# The behavioral targets each run observed, straight from the diff.
reference_targets() {
    jq -r '.behavior_diff.deltas[]
           | select(.change == "shared" or .change == "removed")
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
echo "REFERENCE"
while read -r target; do
    [ -n "$target" ] && printf '  %-22s observed\n' "$target"
done < <(reference_targets)
echo
echo "CANDIDATE"
while read -r target; do
    [ -n "$target" ] && printf '  %-22s observed\n' "$target"
done < <(reference_targets)
while read -r target; do
    [ -n "$target" ] && printf '  %-22s NEW\n' "$target"
done < <(added_targets)
echo
echo "COMPARISON"
echo "  Added behaviors: $ADDED"
echo "  Gate: $(echo "$VERDICT" | tr '[:lower:]' '[:upper:]')"
echo "  Limit: max-added-behaviors = $LIMIT (actual $ACTUAL)"
echo "  compare exit code: $COMPARE_STATUS"
echo

if [ "$COMPARE_STATUS" -eq 1 ] && [ "$VERDICT" = "fail" ]; then
    cat <<'NOTE'
  This FAIL is the expected outcome. The candidate introduced one behavior
  the reference never showed, and the limit supplied was zero. It is a
  deterministic result under the limits chosen for this demo — not a finding
  that the candidate is unsafe, malicious or compromised.
NOTE
else
    echo "  Unexpected: this demo expects a gate FAIL with exit 1."
fi

echo
echo "Inspect:"
echo "  Reference run: $REFERENCE_RUN"
echo "  Candidate run: $CANDIDATE_RUN"
echo
echo "Open:"
echo "  $API_URL/"
echo "  then: Open evaluation by ID -> $CANDIDATE_RUN"
echo
echo "Ctrl-C to stop."
echo

# The runtime stays up so the WebUI is usable. `wait` blocks until a signal,
# and the trap installed by demo_init cleans every child on the way out.
while true; do
    wait || break
done

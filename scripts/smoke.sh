#!/usr/bin/env bash
#
# The same lifecycle as demo.sh, asserted rather than narrated, and it exits
# instead of waiting for Ctrl-C.

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

demo_init

FAILURES=0
check() {
    local what="$1"; shift
    if "$@"; then
        printf '  ok    %s\n' "$what"
    else
        printf '  FAIL  %s\n' "$what"
        FAILURES=$(( FAILURES + 1 ))
    fi
}

echo
echo "Trustvian Python Agent Demo — smoke test"
echo

# ---------------------------------------------------------------------
# Application isolation. Checked first, because it needs nothing running
# and it is the claim the whole demo exists to make.
#
# agent/main.py no longer merely avoids *importing* either project — the
# words "trustvian" and "opentelemetry" do not appear anywhere in the file
# at all, including comments and docstrings, because a narrated claim of
# isolation would undercut the isolation itself. So the check here is a
# plain whole-file search, not an import- or call-shaped grep: it is both
# simpler and a better proof than the narrower pattern would be.
#
# If someone legitimately needs to mention either project in a comment in
# agent/main.py later (e.g. explaining why it must stay uninstrumented),
# that person is choosing to weaken this exact check, and should do so
# deliberately here rather than let it happen by accident.
# ---------------------------------------------------------------------
echo "Application isolation"

# grep exits 1 when it read the file and matched nothing, and 2 when it
# could not open the file at all — `!` only inverts zero-versus-nonzero, so
# a bare `! grep ...` collapses "clean" and "unreadable" into the same
# passing result. Guard on readability first, explicitly, so a missing or
# unreadable file is reported as exactly that rather than as a pass.
no_forbidden_mentions() {
    local target="$DEMO_ROOT/agent/main.py"
    if [ ! -r "$target" ]; then
        printf '        agent/main.py is missing or unreadable at %s\n' "$target" >&2
        return 1
    fi
    ! grep -nEi '(trustvian|opentelemetry)' "$target"
}
no_forbidden_dependencies() {
    local target="$DEMO_ROOT/agent/requirements.txt"
    if [ ! -r "$target" ]; then
        printf '        agent/requirements.txt is missing or unreadable at %s\n' "$target" >&2
        return 1
    fi
    ! grep -nEi '^[[:space:]]*(trustvian|opentelemetry)' "$target"
}

check "agent/main.py mentions neither trustvian nor opentelemetry, anywhere in the file" no_forbidden_mentions
check "agent/requirements.txt declares neither"                                          no_forbidden_dependencies

if [ "$FAILURES" -ne 0 ]; then
    echo
    echo "Application isolation failed; not running the rest."
    exit 1
fi

# ---------------------------------------------------------------------
# The full lifecycle.
# ---------------------------------------------------------------------
echo
echo "Lifecycle"

"$DEMO_ROOT/scripts/bootstrap.sh" >/dev/null
log "bootstrap"

start_runtime
log "runtime at $API_URL"

start_mocks
log "mock services on $MOCK_PORT"

create_control_plane
log "control-plane objects created"

run_evaluation "$REFERENCE_RUN" "$REFERENCE_CANDIDATE" "$REFERENCE_PROFILE" \
               "reference" "$REFERENCE_ACTIONS"
log "reference run complete"

run_evaluation "$CANDIDATE_RUN" "$CANDIDATE_CANDIDATE" "$CANDIDATE_PROFILE" \
               "candidate" "$CANDIDATE_ACTIONS"
log "candidate run complete"

compare_runs
log "comparison written to $COMPARISON_FILE"

# ---------------------------------------------------------------------
# Assertions, all against authoritative control-plane responses. Counts
# are asserted exactly (-eq), never as lower bounds: the demo is
# deterministic, and a smoke test that accepts "at least some records"
# would still pass on a demo that had silently half-broken.
# ---------------------------------------------------------------------
echo
echo "Assertions"

REFERENCE_EXPECTED=$(( ROUNDS * TICKETS_PER_ROUND * REFERENCE_ACTIONS ))
CANDIDATE_EXPECTED=$(( ROUNDS * TICKETS_PER_ROUND * CANDIDATE_ACTIONS ))

reference_has_evidence() { [ "$(record_count "$REFERENCE_RUN")" -eq "$REFERENCE_EXPECTED" ]; }
candidate_has_evidence() { [ "$(record_count "$CANDIDATE_RUN")" -eq "$CANDIDATE_EXPECTED" ]; }
reference_completed()    { [ "$(tv eval get --id "$REFERENCE_RUN" --json | jq -r .status)" = "completed" ]; }
candidate_completed()    { [ "$(tv eval get --id "$CANDIDATE_RUN" --json | jq -r .status)" = "completed" ]; }

reference_behaviors()    { [ "$(distinct_behaviors "$REFERENCE_RUN")" -eq 3 ]; }
candidate_behaviors()    { [ "$(distinct_behaviors "$CANDIDATE_RUN")" -eq 4 ]; }

comparison_is_valid() {
    jq -e '.version == "1"
           and (.behavior_diff | type == "object")
           and (.scorecard    | type == "object")
           and (.gate.verdict | type == "string")
           and (.gate.added_behaviors.maximum == "0")' "$COMPARISON_FILE" >/dev/null
}
added_count_is_one()  { [ "$(jq -r '.behavior_diff.added_count' "$COMPARISON_FILE")" -eq 1 ]; }
gate_failed()          { [ "$(jq -r '.gate.verdict' "$COMPARISON_FILE")" = "fail" ]; }
# Exit 1 is the expected gate FAIL this demo produces on purpose. Exit 3
# would be an API or network failure, which compare_runs already
# distinguishes (it calls fail() itself for anything other than 0 or 1) —
# this assertion only needs to confirm the value is exactly 1, never a
# looser "non-zero", so the two outcomes can never be conflated here either.
exit_code_is_one()    { [ "$COMPARE_STATUS" -eq 1 ]; }
added_is_the_export() {
    jq -e '[.behavior_diff.deltas[] | select(.change == "added") | .behavior.target_name]
           | index("export.localhost") != null' "$COMPARISON_FILE" >/dev/null
}

check "reference run holds exactly $REFERENCE_EXPECTED records" reference_has_evidence
check "candidate run holds exactly $CANDIDATE_EXPECTED records" candidate_has_evidence
check "reference run completed"                                 reference_completed
check "candidate run completed"                                 candidate_completed
check "reference observed exactly 3 distinct behaviors"         reference_behaviors
check "candidate observed exactly 4 distinct behaviors"         candidate_behaviors
check "comparison payload is valid"                             comparison_is_valid
check "candidate added exactly one behavior"                    added_count_is_one
check "the added behavior is the customer export"               added_is_the_export
check "gate verdict is fail"                                    gate_failed
check "eval compare exited 1"                                   exit_code_is_one

# ---------------------------------------------------------------------
# Process hygiene. Checked by shutting everything down here rather than
# leaving it to the trap, so the result is observable.
# ---------------------------------------------------------------------
echo
echo "Cleanup"

survivors=0
if [ "${#CHILD_PIDS[@]}" -gt 0 ]; then
    for pid in "${CHILD_PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    for pid in "${CHILD_PIDS[@]}"; do
        wait "$pid" 2>/dev/null || true
    done
    for pid in "${CHILD_PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then survivors=$(( survivors + 1 )); fi
    done
fi
no_survivors() { [ "$survivors" -eq 0 ]; }
check "every spawned process exited" no_survivors

echo
if [ "$FAILURES" -ne 0 ]; then
    echo "smoke: $FAILURES check(s) failed"
    exit 1
fi
echo "smoke: all checks passed"

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
# The application files no longer merely avoid *importing* either project —
# the words "trustvian" and "opentelemetry" do not appear anywhere in them
# at all, including comments and docstrings, because a narrated claim of
# isolation would undercut the isolation itself. So the check here is a
# plain whole-file search, not an import- or call-shaped grep: it is both
# simpler and a better proof than the narrower pattern would be.
#
# If someone legitimately needs to mention either project in a comment in
# one of these files later (e.g. explaining why it must stay
# uninstrumented), that person is choosing to weaken this exact check, and
# should do so deliberately here rather than let it happen by accident.
# ---------------------------------------------------------------------
echo "Application isolation"

# Explicit rather than a glob: adding an application file to this set is a
# decision someone makes here, not something a new file inherits silently.
APPLICATION_SOURCES="agent/main.py agent/planner.py agent/tools.py \
agent/__init__.py fixtures/deterministic_agent.py"

# grep exits 1 when it read the file and matched nothing, and 2 when it
# could not open the file at all — `!` only inverts zero-versus-nonzero, so
# a bare `! grep ...` collapses "clean" and "unreadable" into the same
# passing result. Guard on readability first, explicitly, so a missing or
# unreadable file is reported as exactly that rather than as a pass.
no_forbidden_mentions() {
    local target path rc=0
    for target in $APPLICATION_SOURCES; do
        path="$DEMO_ROOT/$target"
        if [ ! -r "$path" ]; then
            printf '        %s is missing or unreadable at %s\n' "$target" "$path" >&2
            rc=1
            continue
        fi
        if grep -nEi '(trustvian|opentelemetry)' "$path"; then
            printf '        %s mentions a forbidden name above\n' "$target" >&2
            rc=1
        fi
    done
    return "$rc"
}
no_forbidden_dependencies() {
    local target="$DEMO_ROOT/agent/requirements.txt"
    if [ ! -r "$target" ]; then
        printf '        agent/requirements.txt is missing or unreadable at %s\n' "$target" >&2
        return 1
    fi
    ! grep -nEi '^[[:space:]]*(trustvian|opentelemetry)' "$target"
}

# tools/ is this repository's own tooling — a scenario runner, aggregation,
# report rendering. It is not the application, it runs from a different
# virtualenv, and nothing the agent runs may reach it. A fixture that imported
# the runner would put a YAML parser and a control-plane client on the
# application's import path, which is exactly what the two checks above exist
# to prevent.
no_tooling_on_the_agents_import_path() {
    local target path rc=0
    for target in $APPLICATION_SOURCES; do
        path="$DEMO_ROOT/$target"
        [ -r "$path" ] || continue
        if grep -nE '^[[:space:]]*(import|from)[[:space:]]+tvdemo' "$path"; then
            printf '        %s imports the tooling package above\n' "$target" >&2
            rc=1
        fi
    done
    return "$rc"
}

check "application sources mention neither trustvian nor opentelemetry, anywhere" no_forbidden_mentions
check "agent/requirements.txt declares neither"                                          no_forbidden_dependencies
check "no application source imports this repository's tooling"                          no_tooling_on_the_agents_import_path

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

# The tests need the demo venv for `requests`, so they run after bootstrap —
# but before anything is started, because a logic error in the agent should
# cost seconds rather than a whole lifecycle.
unit_tests_pass() {
    "$VENV_DIR/bin/python" -m unittest discover -s "$DEMO_ROOT/tests" -q
}
check "agent unit tests pass" unit_tests_pass

# Two suites, two interpreters, on purpose. The agent's tests need `requests`
# and the tooling's need PyYAML, and no single environment has both — which is
# the boundary those environments exist to draw. A suite that could only run
# from an interpreter holding everything would be quietly asserting the
# opposite.
tooling_tests_pass() {
    "$TOOLS_VENV_DIR/bin/python" -m unittest discover -s "$DEMO_ROOT/tools/tests" -q
}
check "tooling unit tests pass" tooling_tests_pass

if [ "$FAILURES" -ne 0 ]; then
    echo
    echo "Unit tests failed; not running the lifecycle."
    exit 1
fi

start_runtime
log "runtime at $API_URL"

start_mocks
log "mock services on $MOCK_PORT"

REFERENCE_EXPECTED=$(( ROUNDS * TICKETS_PER_ROUND * REFERENCE_ACTIONS ))
CANDIDATE_EXPECTED=$(( ROUNDS * TICKETS_PER_ROUND * CANDIDATE_ACTIONS ))

# Both runs go through the same wrapper `make demo` uses, with the same
# options. A smoke test that drove a private code path would be asserting
# something nobody runs.
#
# The fixture's activity is fixed, so the expected count is stated
# arithmetically rather than read back from the workload's own report: that is
# the stronger check here, because it would catch a fixture that silently did
# less. The model-driven agent gets --expect-records-from instead, since its
# activity is not knowable in advance.
smoke_run() {
    local run_id="$1" candidate="$2" profile="$3" mode="$4" expected="$5"
    SUPPORT_AGENT_PORT="$MOCK_PORT" \
    SUPPORT_AGENT_MODE="$mode" \
    SUPPORT_AGENT_ROUNDS="$ROUNDS" \
        "$DEMO_ROOT/scripts/tv-dev.sh" \
            --api-url "$API_URL" \
            --run-id "$run_id" \
            --candidate "$candidate" \
            --behavioral-profile "$profile" \
            --expect-records "$expected" \
            -- "$VENV_DIR/bin/python" "$DEMO_ROOT/fixtures/deterministic_agent.py"
}

smoke_run "$REFERENCE_RUN" "$REFERENCE_CANDIDATE" "$REFERENCE_PROFILE" \
          "reference" "$REFERENCE_EXPECTED"
log "reference run complete"

smoke_run "$CANDIDATE_RUN" "$CANDIDATE_CANDIDATE" "$CANDIDATE_PROFILE" \
          "candidate" "$CANDIDATE_EXPECTED"
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
    # Every PID above has now been killed and reaped by hand. Clear the
    # tracking array so the EXIT trap's cleanup() does not signal them a
    # second time — exactly the recycled-PID hazard untrack() exists to
    # prevent: by the time the trap runs, the OS may have handed one of
    # these numbers to an unrelated process.
    CHILD_PIDS=()
fi
no_survivors() { [ "$survivors" -eq 0 ]; }
check "every spawned process exited" no_survivors

echo
if [ "$FAILURES" -ne 0 ]; then
    echo "smoke: $FAILURES check(s) failed"
    exit 1
fi
echo "smoke: all checks passed"

#!/usr/bin/env bash
#
# The demo lifecycle, shared by demo.sh and smoke.sh.
#
# It lives in one file because those two drive the identical sequence and
# differ only in what they do at the end. Two copies of a twenty-step
# orchestration is two orchestrations, and the one a user trusts is whichever
# they happened to run.
#
# No sleep is a correctness primitive anywhere in here. Every wait names the
# observable condition it is waiting for: a bound socket, an HTTP status, a
# published discovery file, or an authoritative record count.

# --- identifiers, fixed so the README can name them ------------------
PROJECT_ID="support-demo"
PROJECT_NAME="Support Demo"
AGENT_ID="support-agent"
AGENT_NAME="Support Agent"
REFERENCE_CANDIDATE="reference"
CANDIDATE_CANDIDATE="candidate"
REFERENCE_RUN="run-reference"
CANDIDATE_RUN="run-candidate"
REFERENCE_PROFILE="support-reference"
CANDIDATE_PROFILE="support-candidate"

# The environment must match what the telemetry reports, or the control plane
# refuses the record: a run's environment and DecisionRecord.Environment are
# checked against each other, and two runs in different environments cannot
# be compared at all.
ENVIRONMENT="local"

# Three tickets per round; the agent loops SUPPORT_AGENT_ROUNDS rounds.
ROUNDS="${SUPPORT_AGENT_ROUNDS:-3}"
TICKETS_PER_ROUND=3
REFERENCE_ACTIONS=3   # CRM, Knowledge, Mail
CANDIDATE_ACTIONS=4   # CRM, Knowledge, Export, Mail

# CHILD_PIDS holds every process this script started, in start order.
# RUNTIME_PID is tracked separately because start_runtime needs to notice its
# own child dying during the startup poll, and bash 3.2 has no ${a[-1]}.
CHILD_PIDS=()
RUNTIME_PID=""

demo_init() {
    DEMO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    BIN_DIR="$DEMO_ROOT/.demo/bin"
    VENV_DIR="$DEMO_ROOT/.demo/venv"
    STATE_DIR="$DEMO_ROOT/.trustvian"
    RUNTIME_DIR="$DEMO_ROOT/.runtime"
    mkdir -p "$RUNTIME_DIR"
    cd "$DEMO_ROOT"

    trap cleanup EXIT INT TERM
}

log()  { printf '  %s\n' "$*"; }
fail() { printf '\nerror: %s\n' "$*" >&2; exit 1; }

# track PID records a child so cleanup can reach it even if it outlives its
# shell function.
track() { CHILD_PIDS+=("$1"); }

cleanup() {
    local status=$?
    trap - EXIT INT TERM

    # bash 3.2 treats an empty array as unset under `set -u`, so every
    # expansion below is guarded rather than assumed non-empty.
    if [ "${#CHILD_PIDS[@]}" -gt 0 ]; then
        # Reverse order, so the collector stops before the runtime it posts
        # to — a collector outliving its control plane logs a wall of
        # connection failures on the way out.
        local i pid
        for (( i=${#CHILD_PIDS[@]}-1 ; i>=0 ; i-- )); do
            pid="${CHILD_PIDS[$i]}"
            kill "$pid" 2>/dev/null || true
        done
        # `wait` rather than sleep: it returns as soon as the child is
        # reaped, so a clean shutdown costs nothing.
        for pid in "${CHILD_PIDS[@]}"; do
            wait "$pid" 2>/dev/null || true
        done
        for pid in "${CHILD_PIDS[@]}"; do
            if kill -0 "$pid" 2>/dev/null; then
                kill -9 "$pid" 2>/dev/null || true
            fi
        done
    fi
    exit "$status"
}

# free_port asks the OS for an unused port. It is inherently advisory — the
# port can be taken between here and the bind — so every caller verifies the
# bind actually happened rather than assuming it did.
free_port() {
    python3 -c 'import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()'
}

wait_tcp() {
    local host="$1" port="$2" what="$3" timeout="${4:-30}"
    local deadline=$(( SECONDS + timeout ))
    while (( SECONDS < deadline )); do
        if python3 -c 'import socket,sys
s = socket.socket()
s.settimeout(1)
sys.exit(0 if s.connect_ex((sys.argv[1], int(sys.argv[2]))) == 0 else 1)' "$host" "$port" 2>/dev/null; then
            return 0
        fi
        sleep 0.1
    done
    fail "timed out after ${timeout}s waiting for $what on $host:$port"
}

wait_http() {
    local url="$1" what="$2" timeout="${3:-30}"
    local deadline=$(( SECONDS + timeout ))
    while (( SECONDS < deadline )); do
        if curl -fsS -o /dev/null --max-time 2 "$url" 2>/dev/null; then
            return 0
        fi
        sleep 0.1
    done
    fail "timed out after ${timeout}s waiting for $what at $url"
}

# --- Trustvian local runtime -----------------------------------------

start_runtime() {
    # A fresh database every run. Reusing one means the second `make demo`
    # hits already_exists on creation and, worse, tries to start a run that
    # already completed — leaving a state the user cannot proceed from.
    rm -rf "$STATE_DIR"

    "$BIN_DIR/trustvian-local" --state-dir "$STATE_DIR" \
        >"$RUNTIME_DIR/trustvian-local.log" 2>&1 &
    RUNTIME_PID=$!
    track "$RUNTIME_PID"

    # The discovery file is published only after the listener binds, which is
    # exactly what makes it the readiness signal rather than a guess.
    local discovery="$STATE_DIR/runtime.json"
    local deadline=$(( SECONDS + 30 ))
    while (( SECONDS < deadline )); do
        if [ -s "$discovery" ]; then
            API_URL="$(jq -r '.api_url // empty' "$discovery" 2>/dev/null || true)"
            if [ -n "$API_URL" ]; then break; fi
        fi
        if ! kill -0 "$RUNTIME_PID" 2>/dev/null; then
            fail "trustvian-local exited during startup:
$(cat "$RUNTIME_DIR/trustvian-local.log")"
        fi
        sleep 0.1
    done
    [ -n "${API_URL:-}" ] || fail "trustvian-local never published $discovery"

    # Prove the endpoint answers, not just that the file exists. A 404 from
    # /v1 on an unknown project is a healthy server.
    local deadline2=$(( SECONDS + 30 ))
    while (( SECONDS < deadline2 )); do
        local code
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 \
                "$API_URL/v1/projects/__probe__" || true)"
        if [ "$code" = "404" ] || [ "$code" = "200" ]; then return 0; fi
        sleep 0.1
    done
    fail "the local runtime at $API_URL never answered /v1"
}

# tv runs the real Trustvian CLI against the discovered control plane.
# Every control-plane object in this demo is created through it — nothing
# writes to the database directly.
tv() { "$BIN_DIR/trustvian" --api-url "$API_URL" "$@"; }

# --- mock services ----------------------------------------------------

start_mocks() {
    MOCK_PORT="$(free_port)"
    "$VENV_DIR/bin/python" "$DEMO_ROOT/mock_services/server.py" --port "$MOCK_PORT" \
        >"$RUNTIME_DIR/mocks.log" 2>&1 &
    track $!
    wait_http "http://127.0.0.1:$MOCK_PORT/healthz" "the mock services" 30
}

# --- Trustvian Collector ---------------------------------------------

COLLECTOR_PID=""

start_collector() {
    local run_id="$1" profile="$2"

    OTLP_PORT="$(free_port)"
    local health_port; health_port="$(free_port)"
    local config="$RUNTIME_DIR/collector-$run_id.yaml"

    cat >"$config" <<YAML
# Generated by scripts/lib.sh. One Collector process per evaluation run:
# the processor's configuration compiles once and is fixed for its lifetime,
# so feeding a different run means a new process.
receivers:
  otlp:
    protocols:
      http:
        endpoint: 127.0.0.1:$OTLP_PORT

processors:
  trustvian:
    health:
      endpoint: 127.0.0.1:$health_port
      readiness_timeout: 2s
    evaluation:
      api_url: $API_URL
      run_id: $run_id
      behavioral_profile: $profile
      required: true

exporters:
  debug:
    verbosity: basic

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [trustvian]
      exporters: [debug]
YAML

    "$BIN_DIR/trustvian-collector" "--config=$config" \
        >"$RUNTIME_DIR/collector-$run_id.log" 2>&1 &
    COLLECTOR_PID=$!
    track "$COLLECTOR_PID"

    # /livez is the processor's own readiness surface. If the Collector
    # refused its config — an unreachable control plane, a run that is not
    # running — it exits instead, and that is reported as itself.
    local deadline=$(( SECONDS + 45 ))
    while (( SECONDS < deadline )); do
        if curl -fsS -o /dev/null --max-time 2 "http://127.0.0.1:$health_port/livez" 2>/dev/null; then
            wait_tcp 127.0.0.1 "$OTLP_PORT" "the Collector's OTLP receiver" 15
            return 0
        fi
        if ! kill -0 "$COLLECTOR_PID" 2>/dev/null; then
            fail "the Trustvian Collector exited during startup:
$(tail -20 "$RUNTIME_DIR/collector-$run_id.log")"
        fi
        sleep 0.1
    done
    fail "the Trustvian Collector never became live:
$(tail -20 "$RUNTIME_DIR/collector-$run_id.log")"
}

stop_collector() {
    [ -n "$COLLECTOR_PID" ] || return 0
    kill "$COLLECTOR_PID" 2>/dev/null || true
    wait "$COLLECTOR_PID" 2>/dev/null || true
    COLLECTOR_PID=""
}

# --- the agent, under runtime instrumentation ------------------------

run_agent() {
    local mode="$1" log="$RUNTIME_DIR/agent-$mode.log"

    # Every OTEL_* variable is supplied here, at launch. None of them is
    # referenced by the application, and none is written into its manifest.
    #
    # OTEL_SEMCONV_STABILITY_OPT_IN=http is not optional. Without it the
    # instrumentation emits the legacy http.url/http.method attributes, while
    # Trustvian's processor reads server.address and http.request.method —
    # so every span would arrive with an empty target and all four actions
    # would collapse into two behaviors.
    SUPPORT_AGENT_PORT="$MOCK_PORT" \
    SUPPORT_AGENT_MODE="$mode" \
    SUPPORT_AGENT_ROUNDS="$ROUNDS" \
    OTEL_SERVICE_NAME="support-agent" \
    OTEL_RESOURCE_ATTRIBUTES="deployment.environment.name=$ENVIRONMENT" \
    OTEL_SEMCONV_STABILITY_OPT_IN="http" \
    OTEL_TRACES_EXPORTER="otlp" \
    OTEL_METRICS_EXPORTER="none" \
    OTEL_LOGS_EXPORTER="none" \
    OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf" \
    OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:$OTLP_PORT" \
    OTEL_BSP_SCHEDULE_DELAY="200" \
        "$VENV_DIR/bin/opentelemetry-instrument" \
        "$VENV_DIR/bin/python" "$DEMO_ROOT/agent/main.py" \
        >"$log" 2>&1 \
        || fail "the $mode agent run failed:
$(tail -20 "$log")"
}

# --- evaluation runs --------------------------------------------------

create_control_plane() {
    tv project create   --id "$PROJECT_ID" --name "$PROJECT_NAME" >/dev/null
    tv agent create     --id "$AGENT_ID" --project-id "$PROJECT_ID" --name "$AGENT_NAME" >/dev/null
    tv candidate create --id "$REFERENCE_CANDIDATE" --agent-id "$AGENT_ID" \
        --label "reference behavior" >/dev/null
    tv candidate create --id "$CANDIDATE_CANDIDATE" --agent-id "$AGENT_ID" \
        --label "adds customer export" >/dev/null
}

# record_count reads the authoritative count from the control plane. Never
# from the Collector's log, and never accumulated locally.
record_count() {
    tv eval progress --id "$1" --json | jq -r '.record_count'
}

distinct_behaviors() {
    tv eval progress --id "$1" --json | jq -r '.distinct_behavior_count'
}

# wait_for_records blocks until the run holds at least want records.
#
# This is what replaces a sleep after the agent exits: the Python SDK flushes
# its span batch at process exit, the Collector then analyzes and posts each
# one, and the only authoritative signal that the evidence landed is the
# control plane's own count.
wait_for_records() {
    local run_id="$1" want="$2" timeout="${3:-60}"
    local deadline=$(( SECONDS + timeout )) got=0
    while (( SECONDS < deadline )); do
        got="$(record_count "$run_id")"
        if [ "$got" -ge "$want" ] 2>/dev/null; then return 0; fi
        sleep 0.2
    done
    fail "run $run_id holds $got records after ${timeout}s, expected at least $want
       Collector log: $RUNTIME_DIR/collector-$run_id.log"
}

run_evaluation() {
    local run_id="$1" candidate_id="$2" profile="$3" mode="$4" actions="$5"
    local expected=$(( ROUNDS * TICKETS_PER_ROUND * actions ))

    tv eval create --id "$run_id" --candidate-id "$candidate_id" \
        --environment "$ENVIRONMENT" --behavioral-profile "$profile" >/dev/null
    tv eval start --id "$run_id" >/dev/null

    # The Collector is started after the run is running, because ingest is
    # refused for a pending run and the sink reads its cursor at startup.
    start_collector "$run_id" "$profile"
    run_agent "$mode"
    wait_for_records "$run_id" "$expected"

    # Stopped before the run completes: ingest is refused once a run is
    # terminal, so a late span would fail the batch rather than be ignored.
    stop_collector
    tv eval complete --id "$run_id" >/dev/null
}

# --- comparison -------------------------------------------------------

# compare_runs performs the real server-side comparison and records the exit
# code. Exit 1 is the gate FAIL this demo expects; exit 3 is an API or
# network failure and must never be reported as a gate result.
compare_runs() {
    COMPARISON_FILE="$DEMO_ROOT/.demo/comparison.json"
    set +e
    tv eval compare \
        --reference-run "$REFERENCE_RUN" \
        --candidate-run "$CANDIDATE_RUN" \
        --max-added-behaviors 0 \
        --max-block-decisions 0 \
        --max-critical-risk-observations 0 \
        --json >"$COMPARISON_FILE" 2>"$RUNTIME_DIR/compare.err"
    COMPARE_STATUS=$?
    set -e

    if [ "$COMPARE_STATUS" -ne 0 ] && [ "$COMPARE_STATUS" -ne 1 ]; then
        fail "eval compare failed operationally (exit $COMPARE_STATUS), which is not a gate result:
$(cat "$RUNTIME_DIR/compare.err")"
    fi
}

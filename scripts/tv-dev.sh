#!/usr/bin/env bash
#
# tv-dev.sh — run one workload under Trustvian, and produce one evaluation
# run's worth of behavioral evidence from it.
#
#     scripts/tv-dev.sh [options] -- <command...>
#
# This is a stand-in for Trustvian task 077's `trustvian dev`, and it is
# shaped like it deliberately: it composes the runtime, the control-plane
# hierarchy, the Collector and the OpenTelemetry environment around a command
# it does not modify, waits for that command's evidence to land, and completes
# the run. The workload imports nothing from Trustvian or OpenTelemetry, and
# nothing is written into its repository.
#
# It is not `trustvian dev`. It is loopback-only, it knows this demo's layout,
# and it will be deleted when 077 ships — `trustvian_dev_available` in lib.sh
# probes for that and says so on every run.
#
# Two shapes:
#
#     tv-dev.sh runtime up|down|url     the control plane, for callers that
#                                       drive many runs against one
#     tv-dev.sh [options] -- <cmd...>   one evaluation run
#
# With --api-url the wrapper attaches to a control plane someone else started;
# without it, it starts one and stops it on the way out. Task 078 assumes both
# — attaching is the CI path.

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() {
    cat >&2 <<'USAGE'
usage: tv-dev.sh [options] -- <command...>
       tv-dev.sh runtime up|down|url
       tv-dev.sh hierarchy [identity options]

Identity (each defaults to this demo's value; see scripts/lib.sh):
  --project <id>              project to run under
  --agent <id>                agent to run under
  --candidate <id>            candidate this run evaluates
  --environment <ref>         environment ref; must match the telemetry
  --run-id <id>               evaluation run id, unique per invocation
  --behavioral-profile <ref>  learning scope for this run

Evidence:
  --expect-records <n>        wait until the run holds at least n records
  --expect-records-from <f>   read that number from the workload's own run
                              summary (its .http_calls field), for a workload
                              whose activity is not predictable in advance
  --wait-timeout <seconds>    how long to wait for evidence (default 60)

Instrumentation ownership (task 077's modes; absence never selects injection):
  --instrumentation python-zero-code   attach the demo-managed OpenTelemetry
                                       runtime to the child (default)
  --instrumentation existing           configure OTLP only, attach nothing
  --instrumentation none               manage no instrumentation at all

Other:
  --api-url <url>             attach to a running control plane
  --service-name <name>       OTEL_SERVICE_NAME for the child
  --stream                    also send the child's output to the terminal
  --summary-file <path>       exported to the child as SUPPORT_AGENT_SUMMARY
USAGE
    exit 2
}

# --- the control plane, for callers that drive many runs --------------

runtime_pid_file() { printf '%s\n' "$RUNTIME_DIR/trustvian-local.pid"; }

runtime_up() {
    RUNTIME_DETACH="yes"
    start_runtime
    printf '%s\n' "$API_URL"
}

runtime_url() {
    local discovery="$STATE_DIR/runtime.json"
    [ -s "$discovery" ] || fail "no Trustvian runtime is recorded at $discovery.
       Start one with: scripts/tv-dev.sh runtime up"
    jq -r '.api_url // empty' "$discovery"
}

# runtime_down stops only a runtime serving *this* directory's state.
#
# Matched on the state directory rather than on the pid alone: a pid file can
# outlive its process and the number can be recycled, so the command line is
# checked before anything is signalled. A trustvian-local someone started by
# hand for another project has a different --state-dir and is never a
# candidate.
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

# --- the Collector, one process per evaluation run --------------------

COLLECTOR_PID=""

start_collector() {
    local run_id="$1" profile="$2"

    # Allocated together (free_port_pair), not as two separate free_port
    # calls: each of those binds, reads and closes before returning, so
    # nothing reserves the first port while the second is chosen, and a
    # collision was possible. A collision here makes the Collector exit and
    # get reported as "exited during startup" rather than as the port clash
    # it actually is.
    local ports; ports="$(free_port_pair)"
    OTLP_PORT="$(printf '%s\n' "$ports" | sed -n '1p')"
    local health_port; health_port="$(printf '%s\n' "$ports" | sed -n '2p')"
    local config="$RUNTIME_DIR/collector-$run_id.yaml"

    cat >"$config" <<YAML
# Generated by scripts/tv-dev.sh. One Collector process per evaluation run:
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
      # Holds the single record that may be in flight, so a Collector that
      # dies between recording evidence in the control plane and applying
      # that record's learning can tell on restart which of the two already
      # happened. One file per run: the sink refuses to start against a
      # note naming a different run rather than discarding an unsettled
      # record.
      pending_state_path: $RUNTIME_DIR/collector-pending-$run_id.json

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
            # Checked inline, rather than via a bare port wait, so a failure
            # here can tail the Collector's own log. free_port is advisory for
            # both ports it handed out; /livez proves the health port bound,
            # but the OTLP port can still lose a race to something else, and
            # that is exactly the failure a bare timeout would hide.
            local otlp_deadline=$(( SECONDS + 15 ))
            while (( SECONDS < otlp_deadline )); do
                if python3 -c 'import socket,sys
s = socket.socket()
s.settimeout(1)
sys.exit(0 if s.connect_ex((sys.argv[1], int(sys.argv[2]))) == 0 else 1)' 127.0.0.1 "$OTLP_PORT" 2>/dev/null; then
                    return 0
                fi
                sleep 0.1
            done
            fail "the Collector's OTLP receiver never bound on 127.0.0.1:$OTLP_PORT:
$(tail -20 "$RUNTIME_DIR/collector-$run_id.log")"
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
    untrack "$COLLECTOR_PID"
    COLLECTOR_PID=""
}

# --- the control-plane hierarchy, created only where it is missing ----

# ensure_hierarchy is idempotent, because many runs share one control plane.
#
# Existence is asked of the server rather than assumed from a fresh database:
# `get` answers 404 for a missing object, which the CLI reports as exit 3.
# Creating unconditionally would answer 409 already_exists on the second run
# of a stability sweep and take the whole sweep down with it.
ensure_hierarchy() {
    if ! tv project get --id "$PROJECT" >/dev/null 2>&1; then
        tv project create --id "$PROJECT" --name "$PROJECT_NAME" >/dev/null
    fi

    # Created immediately after the project that owns it, and before any run
    # names it. A new environment is active on creation, which is the state a
    # run requires.
    if supports_environments; then
        if ! tv env get --project-id "$PROJECT" --ref "$ENVIRONMENT_REF" >/dev/null 2>&1; then
            tv env create --project-id "$PROJECT" --ref "$ENVIRONMENT_REF" \
                --name "Local" >/dev/null
        fi
    fi

    if ! tv agent get --id "$AGENT" >/dev/null 2>&1; then
        tv agent create --id "$AGENT" --project-id "$PROJECT" --name "$AGENT_NAME" >/dev/null
    fi
    if ! tv candidate get --id "$CANDIDATE" >/dev/null 2>&1; then
        tv candidate create --id "$CANDIDATE" --agent-id "$AGENT" \
            --label "$CANDIDATE" >/dev/null
    fi
}

# --- waiting for evidence ---------------------------------------------

# wait_for_records blocks until the run holds at least want records.
#
# This is what replaces a sleep after the workload exits: the SDK flushes its
# span batch at process exit, the Collector then analyzes and posts each one,
# and the only authoritative signal that the evidence landed is the control
# plane's own count.
wait_for_records() {
    local run_id="$1" want="$2" timeout="${3:-60}"
    local deadline=$(( SECONDS + timeout )) got=0
    while (( SECONDS < deadline )); do
        # If the Collector has died, no further records are ever coming, so
        # this is a hard failure rather than something the timeout should
        # discover a minute later.
        if [ -n "$COLLECTOR_PID" ] && ! kill -0 "$COLLECTOR_PID" 2>/dev/null; then
            fail "the Trustvian Collector exited while waiting for run $run_id's records:
$(tail -20 "$RUNTIME_DIR/collector-$run_id.log")"
        fi

        got="$(record_count "$run_id")"
        # record_count can print "null" (a missing field, schema drift) and
        # still exit 0 — nothing about that looks like a failure until it is
        # used arithmetically below.
        case "$got" in
            ''|*[!0-9]*)
                fail "run $run_id's record count was not a number: got '${got}'
       Collector log:
$(tail -20 "$RUNTIME_DIR/collector-$run_id.log")"
                ;;
        esac
        if [ "$got" -ge "$want" ]; then return 0; fi
        sleep 0.2
    done
    fail "run $run_id holds $got records after ${timeout}s, expected at least $want
       Collector log:
$(tail -20 "$RUNTIME_DIR/collector-$run_id.log")"
}

# expected_records resolves how much evidence to wait for.
#
# A workload whose activity is fixed can state a number. A model-driven one
# cannot — how many turns it takes is not knowable in advance — so it reports
# what it actually did and this reads that back. A missing or unparseable
# summary is a hard failure: waiting on an invented number would turn "the
# workload never finished" into a record-count timeout a minute later.
expected_records() {
    if [ -n "$EXPECT_RECORDS" ]; then
        printf '%s\n' "$EXPECT_RECORDS"
        return 0
    fi
    [ -r "$EXPECT_FROM" ] \
        || fail "the workload wrote no run summary at $EXPECT_FROM, so it did
       not finish. Its log is $CHILD_LOG"
    local calls
    calls="$(jq -r '.http_calls // empty' "$EXPECT_FROM" 2>/dev/null || true)"
    case "$calls" in
        ''|*[!0-9]*)
            fail "the workload's run summary has no usable http_calls:
$(cat "$EXPECT_FROM")" ;;
    esac
    printf '%s\n' "$calls"
}

# --- the child ---------------------------------------------------------

# launch_child runs the workload under the selected instrumentation mode.
#
# Every OTEL_* variable is supplied here, at launch. None of them is
# referenced by the workload, and none is written into its manifest.
#
# OTEL_SEMCONV_STABILITY_OPT_IN=http is not optional. Without it the
# instrumentation emits the legacy http.url/http.method attributes, while
# Trustvian's processor reads server.address and http.request.method — so
# every span would arrive with an empty target and the observed behaviors
# would collapse into fewer than they should.
launch_child() {
    local runner=()
    case "$INSTRUMENTATION" in
        python-zero-code)
            [ -x "$VENV_DIR/bin/opentelemetry-instrument" ] \
                || fail "instrumentation mode python-zero-code was selected, but
       $VENV_DIR/bin/opentelemetry-instrument is not there. Run 'make bootstrap'.
       This mode never half-attaches: nothing is launched."
            runner=("$VENV_DIR/bin/opentelemetry-instrument")
            ;;
        existing|none) runner=() ;;
        *) fail "unknown instrumentation mode '$INSTRUMENTATION'" ;;
    esac

    # The child runs from the directory the caller invoked this script in, and
    # inherits its environment — plus the OTLP variables above and nothing
    # else. Its arguments after -- reach it verbatim.
    (
        cd "$CALLER_PWD"
        if [ -n "$SUMMARY_FILE" ]; then export SUPPORT_AGENT_SUMMARY="$SUMMARY_FILE"; fi
        export PYTHONPATH="${PYTHONPATH:-$DEMO_ROOT}"
        export OTEL_SERVICE_NAME="$SERVICE_NAME"
        export OTEL_RESOURCE_ATTRIBUTES="deployment.environment.name=$ENVIRONMENT_REF"
        export OTEL_SEMCONV_STABILITY_OPT_IN="http"
        export OTEL_TRACES_EXPORTER="otlp"
        export OTEL_METRICS_EXPORTER="none"
        export OTEL_LOGS_EXPORTER="none"
        export OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf"
        export OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:$OTLP_PORT"
        export OTEL_BSP_SCHEDULE_DELAY="200"
        if [ "${#runner[@]}" -gt 0 ]; then
            exec "${runner[@]}" "${CHILD[@]}"
        else
            exec "${CHILD[@]}"
        fi
    ) 2>&1 | agent_output "$CHILD_LOG"
}

# --- defaults ----------------------------------------------------------

PROJECT="$PROJECT_ID"
AGENT="$AGENT_ID"
CANDIDATE="$REFERENCE_CANDIDATE"
ENVIRONMENT_REF="$ENVIRONMENT"
RUN_ID=""
PROFILE=""
SERVICE_NAME="support-agent"
INSTRUMENTATION="python-zero-code"
EXPECT_RECORDS=""
EXPECT_FROM=""
WAIT_TIMEOUT=60
SUMMARY_FILE=""
GIVEN_API_URL=""
CHILD=()

# --- go ----------------------------------------------------------------

SUBCOMMAND=""
case "${1:-}" in
    runtime)
        demo_init
        case "${2:-}" in
            up)   runtime_up ;;
            down) runtime_down ;;
            url)  runtime_url ;;
            *)    usage ;;
        esac
        exit 0
        ;;
    # Creating the hierarchy on its own, before any run exists, is what lets
    # the interactive demo open the browser on a control plane that already
    # knows the project and agent. It is the same idempotent call every run
    # makes, so doing it here changes nothing the runs then do.
    hierarchy) SUBCOMMAND="hierarchy"; shift ;;
esac

while [ $# -gt 0 ]; do
    case "$1" in
        --project)             PROJECT="$2"; shift 2 ;;
        --agent)               AGENT="$2"; shift 2 ;;
        --candidate)           CANDIDATE="$2"; shift 2 ;;
        --environment)         ENVIRONMENT_REF="$2"; shift 2 ;;
        --run-id)              RUN_ID="$2"; shift 2 ;;
        --behavioral-profile)  PROFILE="$2"; shift 2 ;;
        --service-name)        SERVICE_NAME="$2"; shift 2 ;;
        --instrumentation)     INSTRUMENTATION="$2"; shift 2 ;;
        --expect-records)      EXPECT_RECORDS="$2"; shift 2 ;;
        --expect-records-from) EXPECT_FROM="$2"; shift 2 ;;
        --wait-timeout)        WAIT_TIMEOUT="$2"; shift 2 ;;
        --summary-file)        SUMMARY_FILE="$2"; shift 2 ;;
        --api-url)             GIVEN_API_URL="$2"; shift 2 ;;
        --stream)              AGENT_STREAM="yes"; shift ;;
        --help|-h)             usage ;;
        --)                    shift; CHILD=("$@"); break ;;
        *)                     printf 'tv-dev.sh: unknown option %s\n\n' "$1" >&2; usage ;;
    esac
done

if [ "$SUBCOMMAND" = "hierarchy" ]; then
    demo_init
    if [ -n "$GIVEN_API_URL" ]; then API_URL="$GIVEN_API_URL"; else start_runtime; fi
    ensure_hierarchy
    exit 0
fi

[ "${#CHILD[@]}" -gt 0 ] || { printf 'tv-dev.sh: no command given after --\n\n' >&2; usage; }
[ -n "$RUN_ID" ] || { printf 'tv-dev.sh: --run-id is required\n\n' >&2; usage; }
[ -n "$PROFILE" ] || PROFILE="$CANDIDATE"
if [ -n "$EXPECT_RECORDS" ] && [ -n "$EXPECT_FROM" ]; then
    printf 'tv-dev.sh: --expect-records and --expect-records-from are exclusive\n\n' >&2
    usage
fi
if [ -z "$EXPECT_RECORDS" ] && [ -z "$EXPECT_FROM" ]; then
    printf 'tv-dev.sh: one of --expect-records or --expect-records-from is required\n\n' >&2
    usage
fi

demo_init
CHILD_LOG="$RUNTIME_DIR/child-$RUN_ID.log"

# Said once per run, not buried in a doc: this wrapper exists only until the
# real command does.
if trustvian_dev_available; then
    log "note: this Trustvian build ships 'trustvian dev' (task 077)."
    log "      scripts/tv-dev.sh is a stand-in for it and should now be retired."
fi

STARTED_RUNTIME="no"
if [ -n "$GIVEN_API_URL" ]; then
    API_URL="$GIVEN_API_URL"
else
    start_runtime
    STARTED_RUNTIME="yes"
fi

ensure_hierarchy

tv eval create --id "$RUN_ID" --candidate-id "$CANDIDATE" \
    --environment "$ENVIRONMENT_REF" --behavioral-profile "$PROFILE" >/dev/null
tv eval start --id "$RUN_ID" >/dev/null

# The Collector is started after the run is running, because ingest is refused
# for a pending run and the sink reads its cursor at startup.
start_collector "$RUN_ID" "$PROFILE"

# The child's status is its own. A workload that failed did not produce a
# behavioral regression — it produced no verdict at all — so the run is failed
# rather than completed, and nothing downstream can read it as evidence.
CHILD_STATUS=0
set +e
launch_child
CHILD_STATUS=${PIPESTATUS[0]}
set -e

if [ "$CHILD_STATUS" -ne 0 ]; then
    stop_collector
    tv eval fail --id "$RUN_ID" --reason "workload exited $CHILD_STATUS" >/dev/null 2>&1 || true
    fail "the workload exited $CHILD_STATUS; run $RUN_ID was failed, not completed:
$(tail -30 "$CHILD_LOG")"
fi

wait_for_records "$RUN_ID" "$(expected_records)" "$WAIT_TIMEOUT"

# Stopped before the run completes: ingest is refused once a run is terminal,
# so a late span would fail the batch rather than be ignored.
stop_collector
tv eval complete --id "$RUN_ID" >/dev/null

if [ "$STARTED_RUNTIME" = "yes" ]; then
    log "run $RUN_ID complete; this wrapper started the control plane and is stopping it"
fi
exit 0

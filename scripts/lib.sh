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

# fixtures/deterministic_agent.py works three tickets per round, which is
# why smoke records 27. agent/main.py (the model-driven agent) works one
# ticket per round instead (TICKETS[index % len(TICKETS)]), which is why the
# demo records 21 — deliberately: each model-driven ticket costs several
# model round-trips, so three tickets per round would triple those.
# TICKETS_PER_ROUND below describes the fixture only.
ROUNDS="${SUPPORT_AGENT_ROUNDS:-3}"
TICKETS_PER_ROUND=3
REFERENCE_ACTIONS=3   # CRM, Knowledge, Mail
CANDIDATE_ACTIONS=4   # CRM, Knowledge, Export, Mail

# Whether the agent's own transcript reaches the terminal as it happens.
#
# The interactive demo turns this on: watching the model choose each action,
# next to what Trustvian then reports observing, is most of what the demo is
# for. smoke.sh leaves it off — it asserts counts rather than reading a
# transcript, and nine tickets of output would bury its own results. Either
# way the full transcript is written to the log, which is what a failure
# message quotes.
AGENT_STREAM="${AGENT_STREAM:-no}"

# The model this demo is built around. OLLAMA_MODEL overrides it for advanced
# use; everything documented and tested here uses gemma3:4b.
OLLAMA_MODEL_NAME="${OLLAMA_MODEL:-gemma3:4b}"
# Deliberately not the same address the agent calls. This probe wants the
# most reliable address, 127.0.0.1; the agent calls ollama.localhost instead
# because a hostname is what makes the model call legible as its own
# behaviour in the engine's diff (see planner.default_url). Both hardcode
# port 11434, so a non-default Ollama bind fails here as a readiness timeout
# rather than as a named mismatch.
OLLAMA_API="http://127.0.0.1:11434"
OLLAMA_STARTED="no"
OLLAMA_PID=""

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

# pause waits for the developer to press ENTER, and says why when it does not.
#
# Only meaningful with someone watching. With stdin not a terminal — CI, a
# piped run, an automated verification — it reports that and continues, so the
# same script is usable both ways and no unattended caller can hang on it.
# smoke.sh never calls this at all; it is the demo's pacing, not the
# lifecycle's.
pause() {
    local prompt="$1"
    if [ -t 0 ]; then
        printf '\n%s' "$prompt"
        read -r _ || true
        echo
    else
        printf '\n%s\n  (stdin is not a terminal — continuing without waiting)\n' "$prompt"
    fi
}

# live_view_available reports whether the running control plane serves the
# zero-input Live view's collection routes (Trustvian task 074).
#
# Asked of the running server over HTTP rather than grepped out of the sibling
# checkout: a route either answers or it does not, which stays true however the
# checkout is arranged and whatever the source file is called.
#
# `GET /v1/projects` is the discriminator. Today the platform registers only
# `POST` for that exact path, so a GET is refused; task 074 adds the collection
# and it answers 200. Anything else — refused, not found, unreachable — means
# the capability is not there.
live_view_available() {
    local code
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 \
            "$API_URL/v1/projects?limit=1" 2>/dev/null || true)"
    [ "$code" = "200" ]
}

# track PID records a child so cleanup can reach it even if it outlives its
# shell function.
track() { CHILD_PIDS+=("$1"); }

# untrack removes a PID once its owner has already killed and reaped it
# (stop_collector, in particular, since a run's Collector is stopped long
# before the script exits). Without this, a later cleanup would still hold
# that PID and could signal a number the OS has since recycled to an
# unrelated process. Rebuilds the array rather than mutating in place,
# since bash 3.2 has no element removal; the same empty-array-under-`set -u`
# guard applies here, because the rebuilt array may legitimately end up
# empty.
untrack() {
    local target="$1"
    if [ "${#CHILD_PIDS[@]}" -eq 0 ]; then
        return 0
    fi
    local pid kept=()
    for pid in "${CHILD_PIDS[@]}"; do
        if [ "$pid" != "$target" ]; then
            kept+=("$pid")
        fi
    done
    if [ "${#kept[@]}" -gt 0 ]; then
        CHILD_PIDS=("${kept[@]}")
    else
        CHILD_PIDS=()
    fi
}

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

# free_port_pair asks the OS for two DIFFERENT unused ports. Two independent
# free_port calls each bind-then-close before the next runs, so nothing
# reserves the first port while the second is chosen — the OS is free to
# hand the same number back twice. This holds both sockets open at once
# before closing either, which is what actually guarantees they differ.
# Still advisory like free_port, for the same reason.
free_port_pair() {
    python3 -c 'import socket
a = socket.socket(); a.bind(("127.0.0.1", 0))
b = socket.socket(); b.bind(("127.0.0.1", 0))
print(a.getsockname()[1])
print(b.getsockname()[1])
a.close(); b.close()'
}

# wait_http polls a URL until it answers. When pid and log are given, it also
# notices that process dying mid-poll and fails immediately with the log's
# tail, instead of waiting out the full timeout to report a symptom (a port
# that never answers) instead of the cause (in the log the whole time).
wait_http() {
    local url="$1" what="$2" timeout="${3:-30}" pid="${4:-}" log="${5:-}"
    local deadline=$(( SECONDS + timeout ))
    while (( SECONDS < deadline )); do
        if curl -fsS -o /dev/null --max-time 2 "$url" 2>/dev/null; then
            return 0
        fi
        if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
            if [ -n "$log" ]; then
                fail "$what exited during startup:
$(tail -20 "$log")"
            fi
            fail "$what exited during startup"
        fi
        sleep 0.1
    done
    fail "timed out after ${timeout}s waiting for $what at $url"
}

# agent_output writes the agent's stream to its log, and also to the terminal
# when the demo asked for that.
#
# `tee` rather than a second `cat` pass, so the transcript appears turn by turn
# while the model is still working rather than all at once when it finishes —
# the agent already flushes every line for exactly this reason.
#
# Reached through a pipe rather than process substitution, and both callers set
# `pipefail`: a pipeline waits for every member, so by the time a failure is
# reported the log is completely written and safe to quote. Process
# substitution does not wait, and the failure message would sometimes quote a
# half-flushed file.
agent_output() {
    local log="$1"
    if [ "$AGENT_STREAM" = "yes" ]; then
        tee "$log"
    else
        cat >"$log"
    fi
}

# --- Trustvian local runtime -----------------------------------------

start_runtime() {
    # A fresh database every run. Reusing one means the second `make demo`
    # hits already_exists on creation and, worse, tries to start a run that
    # already completed — leaving a state the user cannot proceed from.
    #
    # But trustvian-local has its own guard against this
    # (refuseIfRuntimeIsLive in platform/localruntime/runtime.go): it refuses
    # to start when runtime.json names a reachable endpoint. Deleting the
    # state directory before that guard ever runs means it can never fire —
    # so a second `make demo` in another terminal would silently pull
    # .trustvian/platform.db out from under the first, which keeps serving
    # from the deleted inode with no error anywhere. Check for a live runtime
    # ourselves, first, using the same readiness probe as below.
    #
    # Finding one, we stop it and carry on rather than refusing. That keeps
    # the protection — the danger was ever *sharing* the database, not
    # refusing to start — while sparing the reader a manual step in the one
    # situation this reliably happens: re-running the demo when a previous
    # one is still holding the terminal open for its web UI.
    local prior_discovery="$STATE_DIR/runtime.json"
    if [ -s "$prior_discovery" ]; then
        local prior_url
        prior_url="$(jq -r '.api_url // empty' "$prior_discovery" 2>/dev/null || true)"
        if [ -n "$prior_url" ]; then
            local prior_code
            prior_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 \
                    "$prior_url/v1/projects/__probe__" 2>/dev/null || true)"
            if [ "$prior_code" = "404" ] || [ "$prior_code" = "200" ]; then
                log "a previous demo runtime is live at $prior_url — stopping it"

                # Matched on this demo's own state directory, so it can only
                # ever reach a runtime serving *these* files. An unrelated
                # trustvian-local, started by hand or by another project, has
                # a different --state-dir and is never a candidate.
                #
                # Stopping the runtime is also all that is needed: the other
                # demo.sh is blocked in `wait "$RUNTIME_PID"`, so it returns,
                # runs its EXIT trap, and reaps its own mocks and Collector.
                pkill -TERM -f "trustvian-local --state-dir $STATE_DIR" 2>/dev/null || true

                # Wait for the endpoint to actually stop answering before
                # deleting the directory underneath it — the race this guard
                # exists to prevent would otherwise just move here.
                local gone_by=$(( SECONDS + 15 ))
                while (( SECONDS < gone_by )); do
                    prior_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 \
                            "$prior_url/v1/projects/__probe__" 2>/dev/null || true)"
                    case "$prior_code" in
                        404|200) ;;
                        *) break ;;
                    esac
                    sleep 0.2
                done
                case "$prior_code" in
                    404|200)
                        fail "a Trustvian demo runtime is still live at $prior_url
       after being asked to stop. Stop it by hand (Ctrl-C in its terminal),
       then re-run." ;;
                esac
                log "previous runtime stopped"
            fi
        fi
    fi

    rm -rf "$STATE_DIR"

    # The pending-state file and the control-plane evidence it reconciles
    # against share exactly one lifetime. Run ids are fixed strings
    # (run-reference, run-candidate) reused across invocations, so a stale
    # pending file left behind here would describe a run that no longer
    # exists once the control plane above is wiped — and a fresh run reusing
    # that id would inherit a predecessor's unsettled record on its first
    # Collector startup. Discarding the state dir without also discarding
    # these notes would defeat the fix above, not just leave it incomplete.
    rm -f "$RUNTIME_DIR"/collector-pending-*.json

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
#
# --api-url must come after the subcommand, not before it: the CLI parses
# the first token after the binary name as the subcommand itself, so
# `trustvian --api-url <url> project create ...` fails with
# `unknown command "--api-url"`. `trustvian project create ... --api-url
# <url>` works, matching docs/platform-cli.md's worked examples — do not
# "tidy" this back to a leading flag.
tv() { "$BIN_DIR/trustvian" "$@" --api-url "$API_URL"; }

# --- Ollama -------------------------------------------------------------

# ensure_ollama makes a local Ollama with the demo's model available.
#
# It reuses a server that is already running, and only ever stops one it
# started itself: a developer's own Ollama, serving other work, must survive
# this demo. That guarantee is mechanical rather than conditional — the PID is
# tracked only on the branch that started it, so cleanup cannot reach a server
# it did not create.
ensure_ollama() {
    command -v ollama >/dev/null 2>&1 || fail "ollama is not on PATH.
       Install it from https://ollama.com/download, then re-run \`make demo\`.
       \`make smoke\` needs no model and does not require Ollama."

    if curl -fsS -o /dev/null --max-time 2 "$OLLAMA_API/api/version" 2>/dev/null; then
        OLLAMA_STARTED="no"
        log "reusing the Ollama server already running at $OLLAMA_API"
    else
        log "starting ollama serve"
        ollama serve >"$RUNTIME_DIR/ollama.log" 2>&1 &
        OLLAMA_PID=$!
        OLLAMA_STARTED="yes"
        # Tracked only on this branch. A server we did not start is never in
        # CHILD_PIDS, so cleanup cannot signal it.
        track "$OLLAMA_PID"

        local deadline=$(( SECONDS + 60 ))
        while (( SECONDS < deadline )); do
            if curl -fsS -o /dev/null --max-time 2 "$OLLAMA_API/api/version" 2>/dev/null; then
                break
            fi
            if ! kill -0 "$OLLAMA_PID" 2>/dev/null; then
                fail "ollama serve exited during startup:
$(tail -20 "$RUNTIME_DIR/ollama.log")"
            fi
            sleep 0.2
        done
        curl -fsS -o /dev/null --max-time 2 "$OLLAMA_API/api/version" 2>/dev/null \
            || fail "the Ollama API did not become ready at $OLLAMA_API:
$(tail -20 "$RUNTIME_DIR/ollama.log")"
        log "ollama serve is ready"
    fi

    # `ollama list` prints one row per installed model, plus a header. Match
    # the whole first field exactly, so gemma3:4b-something can never satisfy
    # a request for gemma3:4b. -F treats the model name as a fixed string, not
    # a regex — a dotted tag like llama3.2:latest is an ordinary model name,
    # not a wildcard pattern.
    if ollama list 2>/dev/null | awk '{print $1}' | grep -qxF "$OLLAMA_MODEL_NAME"; then
        log "$OLLAMA_MODEL_NAME is already installed"
    else
        echo
        echo "  $OLLAMA_MODEL_NAME is not installed locally."
        echo "  Pulling it with Ollama — a few GB, once."
        echo
        ollama pull "$OLLAMA_MODEL_NAME" \
            || fail "failed to pull $OLLAMA_MODEL_NAME.
       Check your network connection and that the model name is correct."
        log "$OLLAMA_MODEL_NAME pulled"
    fi
}

# --- mock services ----------------------------------------------------

MOCK_PID=""

start_mocks() {
    MOCK_PORT="$(free_port)"
    "$VENV_DIR/bin/python" "$DEMO_ROOT/mock_services/server.py" --port "$MOCK_PORT" \
        >"$RUNTIME_DIR/mocks.log" 2>&1 &
    MOCK_PID=$!
    track "$MOCK_PID"
    wait_http "http://127.0.0.1:$MOCK_PORT/healthz" "the mock services" 30 \
        "$MOCK_PID" "$RUNTIME_DIR/mocks.log"
}

# --- Trustvian Collector ---------------------------------------------

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
            # Checked inline, rather than via wait_tcp, so a failure here can
            # tail the Collector's own log. free_port is advisory for both
            # ports it handed out; /livez proves the health port bound, but
            # the OTLP port can still lose a race to something else, and that
            # is exactly the failure a bare wait_tcp timeout would hide.
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

# --- the agent, under runtime instrumentation ------------------------

run_agent() {
    local mode="$1" target="${2:-agent}" log="$RUNTIME_DIR/agent-$mode.log"

    # The deterministic fixture and the model-driven agent take exactly the
    # same environment and produce the same kind of activity. Which one runs
    # is the caller's choice, not a mode the application knows about.
    local script
    case "$target" in
        agent)   script="$DEMO_ROOT/agent/main.py" ;;
        fixture) script="$DEMO_ROOT/fixtures/deterministic_agent.py" ;;
        *)       fail "run_agent: unknown target '$target'" ;;
    esac

    AGENT_STEPS_FILE="$RUNTIME_DIR/agent-$mode-summary.json"
    rm -f "$AGENT_STEPS_FILE"

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
    SUPPORT_AGENT_SUMMARY="$AGENT_STEPS_FILE" \
    OLLAMA_MODEL="$OLLAMA_MODEL_NAME" \
    PYTHONPATH="$DEMO_ROOT" \
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
        "$VENV_DIR/bin/python" "$script" \
        2>&1 | agent_output "$log" \
        || fail "the $mode agent run failed:
$(tail -30 "$log")"
}

# agent_http_calls reports how many requests the last run made.
#
# The model decides how many turns to take, so this count cannot be computed
# in advance — the agent reports what it actually did. A missing or
# unparseable summary is a hard failure: waiting on an invented number would
# turn "the agent never finished" into a timeout that appears to blame record
# counts a minute later.
agent_http_calls() {
    local mode="$1"
    [ -r "$AGENT_STEPS_FILE" ] \
        || fail "the agent wrote no run summary at $AGENT_STEPS_FILE, so it
       did not finish. Its log is $RUNTIME_DIR/agent-$mode.log"
    local calls
    calls="$(jq -r '.http_calls // empty' "$AGENT_STEPS_FILE" 2>/dev/null || true)"
    case "$calls" in
        ''|*[!0-9]*)
            fail "the agent's run summary has no usable http_calls:
$(cat "$AGENT_STEPS_FILE")" ;;
    esac
    printf '%s\n' "$calls"
}

# agent_steps echoes the actions the model chose, one per line. Unlike the
# count, an unreadable summary is not fatal here: this is narration, and
# agent_http_calls has already failed the run if the summary is missing.
agent_steps() {
    [ -r "$AGENT_STEPS_FILE" ] || return 0
    jq -r '.steps[]?' "$AGENT_STEPS_FILE" 2>/dev/null || true
}

# --- evaluation runs --------------------------------------------------

# supports_environments reports whether the built CLI has the `env` command
# family, by asking the binary itself rather than inferring it from a branch
# name or a version string.
#
# Trustvian's environment model (task 065) makes a run's environment something
# that must already exist: requireUsableEnvironment refuses a run whose
# environment is missing, so a control plane built from a checkout that carries
# it needs one created before the first `eval create`. A checkout without it has
# no `env` command and needs nothing. Asking the binary is what lets this demo
# work against both, which matters because the README and CI target `main`
# while the feature is developed on a branch.
supports_environments() {
    "$BIN_DIR/trustvian" --help 2>&1 | grep -q 'trustvian env'
}

create_control_plane() {
    tv project create   --id "$PROJECT_ID" --name "$PROJECT_NAME" >/dev/null

    # Created immediately after the project that owns it, and before any run
    # names it. A new environment is active on creation, which is the state a
    # run requires.
    if supports_environments; then
        tv env create --project-id "$PROJECT_ID" --ref "$ENVIRONMENT" \
            --name "Local" >/dev/null
        log "environment $ENVIRONMENT created"
    fi

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
        # If the Collector has died, no further records are ever coming, so
        # this is a hard failure rather than something the timeout should
        # discover 60 seconds later — the same liveness check start_runtime
        # and start_collector already make during their own poll loops.
        if [ -n "$COLLECTOR_PID" ] && ! kill -0 "$COLLECTOR_PID" 2>/dev/null; then
            fail "the Trustvian Collector exited while waiting for run $run_id's records:
$(tail -20 "$RUNTIME_DIR/collector-$run_id.log")"
        fi

        got="$(record_count "$run_id")"
        # record_count can print "null" (a missing field, schema drift) and
        # still exit 0 — nothing about that looks like a failure until it is
        # used arithmetically below. Fail on it immediately, naming what
        # came back, rather than let a bad value poll silently to the full
        # timeout.
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

# An evaluation is three phases rather than one call, because the interactive
# demo needs to stop between them: the developer opens the Live view once the
# run exists and the Collector is listening, but *before* any telemetry has
# been produced, so they watch it arrive rather than find it already there.
#
# run_evaluation below still composes all three unchanged, which is what the
# non-interactive smoke path keeps using.

# begin_evaluation establishes the hierarchy and the telemetry path, and
# produces nothing. Afterwards the run is live and the Collector is listening.
begin_evaluation() {
    local run_id="$1" candidate_id="$2" profile="$3"

    tv eval create --id "$run_id" --candidate-id "$candidate_id" \
        --environment "$ENVIRONMENT" --behavioral-profile "$profile" >/dev/null
    tv eval start --id "$run_id" >/dev/null

    # The Collector is started after the run is running, because ingest is
    # refused for a pending run and the sink reads its cursor at startup.
    start_collector "$run_id" "$profile"
}

# observe_agent runs the agent and waits for its evidence to land.
#
# `actions` is consulted only by the arithmetic expected-count path; the
# summary path reads the agent's own report instead.
observe_agent() {
    local run_id="$1" mode="$2" actions="$3"
    local target="${4:-agent}" source="${5:-arithmetic}"

    # A silent fallback here would defeat the point of the summary path: a
    # typo'd or mis-cased source would quietly take the arithmetic branch and
    # predict the wrong expected count for a model-driven run — the exact
    # failure that path exists to remove, reintroduced one layer up.
    case "$source" in
        summary|arithmetic) ;;
        *) fail "observe_agent: unknown expected-count source '$source'" ;;
    esac

    run_agent "$mode" "$target"

    # The fixture's activity is fixed, so arithmetic is the stronger check —
    # it would catch a fixture that silently did less. The model-driven
    # agent's is not, so its own report is the only honest source.
    local expected
    if [ "$source" = "summary" ]; then
        expected="$(agent_http_calls "$mode")"
    else
        expected=$(( ROUNDS * TICKETS_PER_ROUND * actions ))
    fi
    wait_for_records "$run_id" "$expected"
}

# end_evaluation stops the telemetry path and closes the run.
end_evaluation() {
    local run_id="$1"

    # Stopped before the run completes: ingest is refused once a run is
    # terminal, so a late span would fail the batch rather than be ignored.
    stop_collector
    tv eval complete --id "$run_id" >/dev/null
}

# run_evaluation is the whole lifecycle in one call, for callers with nobody
# watching. smoke.sh uses this; demo.sh drives the three phases itself.
run_evaluation() {
    local run_id="$1" candidate_id="$2" profile="$3" mode="$4" actions="$5"
    local target="${6:-agent}" source="${7:-arithmetic}"

    begin_evaluation "$run_id" "$candidate_id" "$profile"
    observe_agent "$run_id" "$mode" "$actions" "$target" "$source"
    end_evaluation "$run_id"
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

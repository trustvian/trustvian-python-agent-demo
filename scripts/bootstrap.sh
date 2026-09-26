#!/usr/bin/env bash
#
# Build the Trustvian binaries from the sibling checkout and create the
# demo-managed Python environment.
#
# Two installs, deliberately separate: the application's declared
# dependencies come from agent/requirements.txt, and the OpenTelemetry
# runtime tooling is installed on top of them without ever entering that
# file. The venv is disposable; the manifest is the application's.

set -euo pipefail

DEMO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TRUSTVIAN_DIR="${TRUSTVIAN_DIR:-$(cd "$DEMO_ROOT/.." && pwd)/trustvian}"
BIN_DIR="$DEMO_ROOT/.demo/bin"
VENV_DIR="$DEMO_ROOT/.demo/venv"

log()  { printf '  %s\n' "$*"; }
fail() { printf '\nerror: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------
# 1. Prerequisites
# ---------------------------------------------------------------------
echo "Checking prerequisites"

command -v go      >/dev/null 2>&1 || fail "go is not on PATH; install Go 1.27 or newer"
command -v python3 >/dev/null 2>&1 || fail "python3 is not on PATH; install it with 'brew install python3' or from https://www.python.org/downloads/"
command -v jq      >/dev/null 2>&1 || fail "jq is not on PATH; install it with 'brew install jq' or 'apt-get install jq'"
command -v curl    >/dev/null 2>&1 || fail "curl is not on PATH; install it with 'brew install curl' or your OS package manager"
log "go, python3, jq, curl"

# Resolution is checked here rather than discovered as a confusing
# NameResolutionError inside the agent, thirty seconds into a demo.
for host in crm.localhost knowledge.localhost mail.localhost export.localhost ollama.localhost; do
    python3 - "$host" <<'PY' || fail "cannot resolve *.localhost hostnames on this machine.
       This demo addresses its mock services by name so each one has a distinct
       identity in telemetry. RFC 6761 reserves .localhost for loopback and
       macOS and systemd Linux both resolve it, but a hardened resolver or a
       minimal container image may not.
       Workaround: add the five names to /etc/hosts pointing at 127.0.0.1."
import socket, sys
socket.getaddrinfo(sys.argv[1], 80, proto=socket.IPPROTO_TCP)
PY
done
log "crm/knowledge/mail/export/ollama .localhost all resolve to loopback"

# ---------------------------------------------------------------------
# 2. The sibling Trustvian checkout
# ---------------------------------------------------------------------
echo "Locating Trustvian"

[ -d "$TRUSTVIAN_DIR" ] || fail "no Trustvian checkout at $TRUSTVIAN_DIR

       This demo builds Trustvian from a sibling checkout. Expected layout:

           trustvian-workspace/
           ├── trustvian/
           └── trustvian-python-agent-demo/   <- you are here

       Clone it beside this repository, or set TRUSTVIAN_DIR to its path."

for marker in go.mod cmd/trustvian platform/cmd/trustvian-local processor/cmd/trustvian-collector; do
    [ -e "$TRUSTVIAN_DIR/$marker" ] || fail "$TRUSTVIAN_DIR does not look like a Trustvian checkout (missing $marker).
       Check that TRUSTVIAN_DIR points at the root of the trustvian repository
       (not a subdirectory of it), or re-clone it there."
done
log "found $TRUSTVIAN_DIR"

# The demo needs the Collector-side evaluation sink. Without it the Collector
# rejects `evaluation:` as an unused config key, and the failure surfaces as
# an opaque startup error several steps later.
if ! grep -q 'mapstructure:"evaluation' "$TRUSTVIAN_DIR/processor/config.go"; then
    fail "the Trustvian checkout at $TRUSTVIAN_DIR has no Collector evaluation sink.

       This demo needs task 073, which adds the processor's evaluation: block.
       It is on Trustvian's main branch — update the checkout:

           git -C $TRUSTVIAN_DIR switch main && git -C $TRUSTVIAN_DIR pull"
fi
log "Collector evaluation ingest is available"

# ---------------------------------------------------------------------
# 3. Build
# ---------------------------------------------------------------------
# Skipped when the three binaries already there were built from the checkout
# as it stands now. `go build` is fast on a warm cache but not free, and this
# script runs at the head of every demo, every smoke test, every scenario and
# every trial of a bench — the sweeps in particular invoke it dozens of times.
#
# The stamp is the checkout's commit plus a dirty marker, not a timestamp. A
# dirty worktree never matches a stamp, so uncommitted Trustvian changes are
# always rebuilt; a clean checkout at the same commit produces the same
# binaries, which is the only case worth skipping.
build_stamp() {
    local head dirty=""
    head="$(git -C "$TRUSTVIAN_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
    if [ -n "$(git -C "$TRUSTVIAN_DIR" status --porcelain 2>/dev/null)" ]; then
        dirty="-dirty-$(date +%s)"
    fi
    printf '%s%s\n' "$head" "$dirty"
}

STAMP_FILE="$BIN_DIR/.build-stamp"
WANT_STAMP="$(build_stamp)"
HAVE_STAMP=""
[ -r "$STAMP_FILE" ] && HAVE_STAMP="$(cat "$STAMP_FILE")"

if [ "$WANT_STAMP" = "$HAVE_STAMP" ] \
   && [ -x "$BIN_DIR/trustvian" ] \
   && [ -x "$BIN_DIR/trustvian-local" ] \
   && [ -x "$BIN_DIR/trustvian-collector" ]; then
    echo "Trustvian binaries are current"
    log "built from ${WANT_STAMP%%-dirty*}"
else
    echo "Building Trustvian binaries"
    mkdir -p "$BIN_DIR"
    rm -f "$STAMP_FILE"

    # GOWORK=off on the nested modules for the same reason Trustvian's own CI
    # uses it: a workspace resolves dependencies the module does not declare.
    ( cd "$TRUSTVIAN_DIR" && go build -o "$BIN_DIR/trustvian" ./cmd/trustvian )
    log "trustvian"
    ( cd "$TRUSTVIAN_DIR/platform" && GOWORK=off go build -o "$BIN_DIR/trustvian-local" ./cmd/trustvian-local )
    log "trustvian-local"
    ( cd "$TRUSTVIAN_DIR/processor" && GOWORK=off go build -o "$BIN_DIR/trustvian-collector" ./cmd/trustvian-collector )
    log "trustvian-collector"

    # Written last, so an interrupted build never leaves a stamp claiming
    # binaries that are not there.
    printf '%s\n' "$WANT_STAMP" >"$STAMP_FILE"
fi

# ---------------------------------------------------------------------
# 4. The demo-managed Python environment
# ---------------------------------------------------------------------
# Also skipped when it is already current, and for the same reason: this
# script runs at the head of everything, and reaching PyPI on every scenario
# trial is both slow and a network dependency nothing here needs twice.
#
# "Current" is a hash over the inputs that decide what gets installed: the
# application's manifest, and this file — which is where the OpenTelemetry
# pins live. Changing either pin therefore reinstalls, without anyone having
# to remember to bump a version number somewhere else.
VENV_RETRY="Try removing the venv and re-running: rm -rf $VENV_DIR && make bootstrap"

hash_of() {
    python3 -c 'import hashlib, sys
h = hashlib.sha256()
for path in sys.argv[1:]:
    with open(path, "rb") as handle:
        h.update(handle.read())
print(h.hexdigest())' "$@"
}

AGENT_REQS_HASH="$(hash_of "$DEMO_ROOT/agent/requirements.txt" "$DEMO_ROOT/scripts/bootstrap.sh")"
AGENT_HASH_FILE="$VENV_DIR/.reqs-hash"

if [ -x "$VENV_DIR/bin/opentelemetry-instrument" ] \
   && [ -r "$AGENT_HASH_FILE" ] \
   && [ "$(cat "$AGENT_HASH_FILE")" = "$AGENT_REQS_HASH" ]; then
    echo "The demo Python environment is current"
    log "application dependencies and the OpenTelemetry runtime are installed"
else
    echo "Creating the demo Python environment"
    rm -f "$AGENT_HASH_FILE"

    if [ ! -x "$VENV_DIR/bin/python" ]; then
        python3 -m venv "$VENV_DIR" || fail "failed to create the virtualenv at $VENV_DIR.
       $VENV_RETRY"
    fi
    "$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip \
        || fail "failed to upgrade pip in $VENV_DIR.
       $VENV_RETRY"

    # Phase 1: the application's own declared dependencies, exactly as written.
    "$VENV_DIR/bin/python" -m pip install --quiet -r "$DEMO_ROOT/agent/requirements.txt" \
        || fail "failed to install the application's dependencies from agent/requirements.txt.
       Check your network connection, then $VENV_RETRY"
    log "application dependencies from agent/requirements.txt"

    # Phase 2: the runtime instrumentation, installed on top and never written
    # back into that file. opentelemetry-bootstrap inspects what is installed
    # and adds the matching instrumentation packages — here, the requests one.
    "$VENV_DIR/bin/python" -m pip install --quiet \
        "opentelemetry-distro==0.65b0" \
        "opentelemetry-exporter-otlp-proto-http==1.44.0" \
        || fail "failed to install the OpenTelemetry runtime tooling.
       Check your network connection, then $VENV_RETRY"
    "$VENV_DIR/bin/opentelemetry-bootstrap" -a install >/dev/null \
        || fail "opentelemetry-bootstrap failed to install instrumentation packages.
       $VENV_RETRY"
    log "OpenTelemetry runtime tooling (not in agent/requirements.txt)"

    [ -x "$VENV_DIR/bin/opentelemetry-instrument" ] \
        || fail "opentelemetry-instrument was not installed into $VENV_DIR.
       $VENV_RETRY"

    "$VENV_DIR/bin/python" -m pip show opentelemetry-instrumentation-requests >/dev/null 2>&1 \
        || fail "opentelemetry-bootstrap did not install the requests instrumentation;
       without it the agent's HTTP calls emit no spans.
       $VENV_RETRY"
    log "requests instrumentation present"

    # Written last, so an interrupted install never leaves a hash claiming an
    # environment that is not complete.
    printf '%s\n' "$AGENT_REQS_HASH" >"$AGENT_HASH_FILE"
fi

# ---------------------------------------------------------------------
# 5. The environment this repository's own tooling runs in
# ---------------------------------------------------------------------
# Separate from the one above, deliberately. The agent's interpreter holds
# exactly its declared dependencies plus the OpenTelemetry runtime; putting a
# YAML parser in there for the benefit of a scenario runner would quietly make
# that claim false. Two virtualenvs cost a second and keep the boundary real.
TOOLS_VENV_DIR="$DEMO_ROOT/.demo/tools-venv"
TOOLS_REQS_HASH="$(hash_of "$DEMO_ROOT/tools/requirements.txt")"
TOOLS_HASH_FILE="$TOOLS_VENV_DIR/.reqs-hash"

if [ -x "$TOOLS_VENV_DIR/bin/python" ] \
   && [ -r "$TOOLS_HASH_FILE" ] \
   && [ "$(cat "$TOOLS_HASH_FILE")" = "$TOOLS_REQS_HASH" ]; then
    echo "The tooling Python environment is current"
else
    echo "Creating the tooling Python environment"
    rm -f "$TOOLS_HASH_FILE"
    if [ ! -x "$TOOLS_VENV_DIR/bin/python" ]; then
        python3 -m venv "$TOOLS_VENV_DIR" || fail "failed to create the virtualenv at $TOOLS_VENV_DIR.
       Try: rm -rf $TOOLS_VENV_DIR && make bootstrap"
    fi
    "$TOOLS_VENV_DIR/bin/python" -m pip install --quiet --upgrade pip \
        || fail "failed to upgrade pip in $TOOLS_VENV_DIR."
    "$TOOLS_VENV_DIR/bin/python" -m pip install --quiet -r "$DEMO_ROOT/tools/requirements.txt" \
        || fail "failed to install this repository's tooling dependencies.
       Check your network connection, then: rm -rf $TOOLS_VENV_DIR && make bootstrap"
    log "tooling dependencies from tools/requirements.txt"
    printf '%s\n' "$TOOLS_REQS_HASH" >"$TOOLS_HASH_FILE"
fi

echo
echo "Bootstrap complete."

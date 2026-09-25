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
echo "Building Trustvian binaries"
mkdir -p "$BIN_DIR"

# GOWORK=off on the nested modules for the same reason Trustvian's own CI
# uses it: a workspace resolves dependencies the module does not declare.
( cd "$TRUSTVIAN_DIR" && go build -o "$BIN_DIR/trustvian" ./cmd/trustvian )
log "trustvian"
( cd "$TRUSTVIAN_DIR/platform" && GOWORK=off go build -o "$BIN_DIR/trustvian-local" ./cmd/trustvian-local )
log "trustvian-local"
( cd "$TRUSTVIAN_DIR/processor" && GOWORK=off go build -o "$BIN_DIR/trustvian-collector" ./cmd/trustvian-collector )
log "trustvian-collector"

# ---------------------------------------------------------------------
# 4. The demo-managed Python environment
# ---------------------------------------------------------------------
echo "Creating the demo Python environment"

VENV_RETRY="Try removing the venv and re-running: rm -rf $VENV_DIR && make bootstrap"

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
# back into that file. opentelemetry-bootstrap inspects what is installed and
# adds the matching instrumentation packages — here, the requests one.
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

echo
echo "Bootstrap complete."

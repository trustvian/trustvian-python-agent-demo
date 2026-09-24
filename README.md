# Trustvian Python Agent Demo

```bash
make demo
```

**The Python application does not import Trustvian or OpenTelemetry.**
OpenTelemetry is attached at runtime, and Trustvian observes the resulting
OTLP telemetry.

`agent/main.py` imports `requests` and nothing else. `agent/requirements.txt`
declares `requests` and nothing else. Neither file mentions either project,
and `make smoke` fails the build if that stops being true.

A cold `make demo` takes a few minutes — it builds three Go binaries, creates
a Python virtual environment and runs four pip install phases before the
first byte of telemetry moves; `make smoke` adds two full evaluation runs on
top of that. That is expected; the command has not hung.

## What this demonstrates

A Python service that knows nothing about Trustvian is observed end to end,
and a real behavioral regression is caught by a real server-side gate — with
no change to the application's source or its dependency manifest.

The application is a small support-ticket agent. In its `reference` mode it
does a CRM lookup, a knowledge-base lookup and sends an email. In its
`candidate` mode it does the same three steps plus one more: it exports the
customer's data before sending the reply. `make demo` runs both modes,
captures the telemetry OpenTelemetry emits for each, and asks Trustvian to
compare the two runs.

## Prerequisites

- Go 1.27+ — builds Trustvian from the sibling checkout
- Python 3.10+ with `venv` — `opentelemetry-sdk`, `opentelemetry-api` and
  `opentelemetry-distro` all declare `Requires-Python: >=3.10`; on 3.9 the
  second bootstrap phase fails, or silently resolves an old distro
- `jq`, `curl`, GNU-compatible `bash`
- IPv6 loopback available — `mock_services/server.py` binds a dual-stack
  socket so `*.localhost` resolves correctly on macOS, which prefers `::1`;
  a host with IPv6 disabled at the kernel will fail to start the mocks
- Network access on every run — `bootstrap.sh`'s pip upgrade and both install
  phases run each time, so PyPI is contacted every run, not just the first.
  Go modules are cached under `GOCACHE`/`GOPATH` after the first run.

No Docker. No API keys. No external service. No `sudo`. Everything the demo
itself generates lives under three gitignored directories in this repo
(`.demo/`, `.runtime/`, `.trustvian/`); no state is written into other
projects. The Go and Python toolchains do write to their usual user-level
caches outside this directory (`GOCACHE`/`GOPATH`, pip's cache) — that's the
standard behavior of `go build` and `pip install`, not something this demo
adds.

## Required layout

```text
trustvian-workspace/
├── trustvian/                      # the Trustvian repository
└── trustvian-python-agent-demo/    # this repository
```

**Until the Collector's evaluation sink merges into Trustvian's `main`, the
sibling checkout must be on `feat/otel-platform-evaluation-sink`.** That is
the current state of Trustvian's `main` as of this writing — `main` alone
does not carry the sink this demo depends on:

```bash
git -C ../trustvian switch feat/otel-platform-evaluation-sink
```

The demo builds Trustvian from `../trustvian`. Set `TRUSTVIAN_DIR` to
override. `scripts/bootstrap.sh` fails immediately, by name, if the checkout
is missing or does not carry the Collector's evaluation sink.

## What `make demo` does

`make demo` runs `scripts/bootstrap.sh`, then:

1. Starts a fresh local Trustvian control plane (`trustvian-local`), backed
   by SQLite, and waits for its API to answer.
2. Starts the mock backend services (`mock_services/server.py`) on
   `crm.localhost`, `knowledge.localhost`, `mail.localhost` and
   `export.localhost`.
3. Creates the project, agent and two candidates through the real
   `trustvian` CLI.
4. Runs the `reference` evaluation: starts a Trustvian Collector, launches
   the agent under `opentelemetry-instrument` in reference mode, waits for
   every record to land, then completes the run.
5. Runs the `candidate` evaluation the same way, in candidate mode.
6. Calls `trustvian eval compare` on the two runs and prints the result.
7. Leaves the control plane running so the WebUI can be inspected, until
   Ctrl-C.

## Architecture

```text
┌ opentelemetry-instrument ────────────────────┐  zero-code instrumentation,
│                                               │  wraps the agent process at
│   agent/main.py                              │  launch — runtime only,
│       │  real HTTP                           │  never imported by it
│       ▼                                      │
└───────┼───────────────────────────────────────┘
        ▼
mock_services/server.py     crm/knowledge/mail/export .localhost
                             plain `python`, not instrumented — the target
                             of the agent's HTTP calls, not their source
        │
        │  OTLP/HTTP  (emitted by the wrapper above, not by the mocks)
        ▼
Trustvian Collector          the trustvian processor
    │
    ▼
Trustvian Engine             real Analyze, real policy, real fingerprints
    │
    ▼
Result.DecisionRecord()
    │  HTTP /v1
    ▼
Trustvian local control plane   SQLite + realtime
    │
    ▼
WebUI · CLI · comparison
```

## The two behaviors

```text
reference   CRM lookup → Knowledge lookup → Send email
candidate   CRM lookup → Knowledge lookup → Export customer data → Send email
```

Each step is a real HTTP request to a distinct hostname, which is what gives
it a distinct identity in telemetry and therefore a distinct behavior in
Trustvian. In a reference run of the demo's default 3 rounds over 3 tickets,
that is 27 records across 3 distinct behaviors (`GET crm.localhost`,
`GET knowledge.localhost`, `POST mail.localhost`); the candidate run is 36
records across 4 (the same three, plus `POST export.localhost`).

### Why `*.localhost` and not `127.0.0.2`–`127.0.0.5`

Telling the four mock services apart by IP address would need addresses
beyond `127.0.0.1`, and binding those on macOS requires
`sudo ifconfig lo0 alias` — a machine-wide, privileged change this demo
deliberately avoids. RFC 6761 reserves the `.localhost` TLD for loopback, and
both macOS and systemd-based Linux resolve arbitrary `*.localhost` names to
loopback with no `/etc/hosts` edit and no DNS query. `bootstrap.sh` checks
this resolution up front and prints a workaround (adding the four names to
`/etc/hosts`) if a resolver does not cooperate.

## Application dependency vs runtime instrumentation

| | Where it lives | In `agent/requirements.txt`? |
|---|---|---|
| `requests` | the application's manifest | yes |
| `opentelemetry-distro`, exporters, instrumentation | `.demo/venv`, installed by `scripts/bootstrap.sh` | **no** |

`bootstrap.sh` installs them in two separate phases for exactly this reason:
phase one installs the application's own dependencies exactly as declared in
`agent/requirements.txt`; phase two installs the OpenTelemetry distro and
exporter on top, then runs `opentelemetry-bootstrap`, which inspects what is
already installed in the environment and adds the matching instrumentation
packages — here, the one for `requests`. That inspection is why the order
matters: it has to run after the application's own dependencies are in
place, and it never writes back into `agent/requirements.txt`.

## The expected result

```text
COMPARISON
  Added behaviors: 1
  Gate: FAIL
  Limit: max-added-behaviors = 0 (actual 1)
  compare exit code: 1
```

The comparison finds 3 shared behaviors, 1 added, 0 removed.

**This FAIL is the expected outcome, and it is not a security verdict.** The
candidate introduced one behavior the reference never showed, and the limit
supplied was zero, so the gate fails deterministically. It does not mean the
candidate is unsafe, malicious or compromised — it means a behavioral change
was detected under the limits chosen for this demo.

`eval compare` exits `1` for a gate FAIL and `3` for an API or network
failure. The demo treats those differently — `scripts/lib.sh`'s
`compare_runs` treats any exit other than `0` or `1` as an operational
failure — and CI consuming this command should draw the same distinction
rather than treating any non-zero exit as a gate result.

## Inspecting the result

Open the printed Web URL (`GET /` returns the WebUI), go to the **Open** tab,
and under **Open by ID** enter one of these in the **Evaluation run ID**
field and click **Open run**:

```text
run-reference
run-candidate
```

`run-reference` shows completed with 27 records and 3 distinct behaviors;
`run-candidate` shows completed with 36 records and 4. There is no list or
search view, by design — the control plane has no collection route, so you
navigate by the IDs you already know. The evidence is durable in SQLite, so
both runs remain retrievable by ID even after the demo process exits.

To reopen the control plane against that same database later, without a
fresh `make demo`:

```bash
.demo/bin/trustvian-local --state-dir .trustvian
```

The next `make demo` still resets `.trustvian/`, so treat this as a way to
revisit existing evidence, not to accumulate it across runs.

## Smoke test

```bash
make smoke
```

Non-interactive, exits zero only when every guarantee holds — 14 checks in
total, starting with the application-isolation check (`agent/main.py` and
`agent/requirements.txt` mention neither Trustvian nor OpenTelemetry) and
ending with confirming that every process the script spawned has exited.

## Cleanup and reset

```bash
make clean
```

Removes `.demo/`, `.runtime/` and `.trustvian/` — binaries, the Python
environment, logs and the evaluation database. `make demo` also resets the
database on each run, so runs never collide.

## Limitations of zero-code instrumentation

Zero-code instrumentation observes the libraries it has instrumentation for.
`opentelemetry-bootstrap` installs the packages matching what is present —
here, `requests` — and each emits spans for the calls it makes.

**It does not understand arbitrary Python business logic.** A function named
`export_customer_data` is not a tool call, a step or an intent as far as
OpenTelemetry is concerned; what is observed is the HTTP request it happens
to make. Trustvian therefore reasons about *the activity the installed
instrumentation reports* — in this demo, an outbound HTTP call to a named
host — and not about the application's internal structure.

That is the right boundary for a demo of telemetry-only observation, and it
is also the honest limit of it. An application that wants richer semantics
emits richer telemetry deliberately, which is a different integration.

One more limitation worth naming: this demo needs
`OTEL_SEMCONV_STABILITY_OPT_IN=http` set at runtime (`scripts/lib.sh` sets it
for every agent launch). Without it, the `requests` instrumentation emits the
older `http.url`/`http.method` attributes, while Trustvian's processor reads
`server.address` and `http.request.method` — so every span would arrive with
an empty target, and the four actions would collapse into two behaviors
instead of three or four. It is a runtime environment variable, so the
application's source and dependency manifest stay clean of it, but anyone
adapting this demo to their own service needs to set it too.

## A note on CI

`.github/workflows/ci.yml` checks out Trustvian at `main` by default, and as
of this writing `main` does not yet carry the Collector's evaluation sink
this demo depends on. Until that work merges, set the repository variable
`TRUSTVIAN_REF` to the branch that carries it
(`feat/otel-platform-evaluation-sink`) for CI to pass; the workflow file
documents this in a comment next to the checkout step.

## Layout

```text
agent/main.py             the application under observation
agent/requirements.txt    its declared dependencies
mock_services/server.py   deterministic local services
scripts/bootstrap.sh      builds Trustvian, creates the demo environment
scripts/lib.sh            the shared lifecycle
scripts/demo.sh           make demo
scripts/smoke.sh          make smoke
```

## License

Apache-2.0. See [LICENSE](LICENSE).

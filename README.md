# Trustvian Python Agent Demo

```bash
make demo
```

Run a real local agent, and watch Trustvian show how its behavior changed
before you ship it.

The agent picks each next action by asking **Ollama `gemma3:4b`** running on
your machine, then performs that action as a real HTTP call. Trustvian watches
the resulting traffic. **The application imports neither Trustvian nor
OpenTelemetry** — instrumentation is attached at runtime.

## The journey

```text
1. Ollama gemma3:4b starts locally (or an already-running one is reused).
2. Trustvian starts locally.
3. The demo prints the Live view URL and waits.
4. You open it and press ENTER.
5. The reference agent runs — the model chooses each action, and the
   observations arrive while you watch.
6. You press ENTER again; the candidate runs, and it can reach one more
   service: export.localhost.
7. Trustvian shows the new behavior.
8. The comparison reports the behavioral difference and the gate result.
```

Trustvian keeps running afterwards so you can look around. Ctrl-C stops it.

```text
local Python agent
    -> local Ollama, gemma3:4b        the model chooses the next action
    -> real local HTTP tool calls
    -> runtime OpenTelemetry instrumentation
    -> Trustvian Collector
    -> Trustvian Engine
    -> local Trustvian control plane
    -> Live WebUI
```

## What this demonstrates

An existing Python application can be observed by Trustvian without being
changed, and a behavioral change between two versions of it shows up as a
difference Trustvian can gate on.

The agent is genuinely agentic: one bounded loop, one model call per turn, and
the model owns action selection. The workflow order is not written in Python —
see [Reference vs candidate](#reference-vs-candidate).

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
- **Ollama** — `ollama` must be on `PATH`. The first run downloads roughly
  3.3 GB for `gemma3:4b`; later runs reuse Ollama's own model cache.

No Docker. No API keys. No external service but Ollama. No `sudo`. Everything
the demo itself generates lives under three gitignored directories in this
repo (`.demo/`, `.runtime/`, `.trustvian/`); no state is written into other
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

The demo builds Trustvian from `../trustvian`. Set `TRUSTVIAN_DIR` to
override. `scripts/bootstrap.sh` fails immediately, by name, if the checkout
is missing or does not carry the Collector's evaluation sink.

## The model

Exactly `gemma3:4b`, at temperature 0. An already-running Ollama server is
reused and **never stopped by the demo**; if none is running, `make demo`
starts one and leaves it running. `OLLAMA_MODEL` overrides the model for
advanced use, but everything documented and tested here uses `gemma3:4b`.

## Reference vs candidate

```text
reference   crm_lookup, knowledge_search, send_email
candidate   crm_lookup, knowledge_search, export_customer, send_email
```

The model is not handed a fixed script — it decides, turn by turn, which of
the tools available to it to call, and when to finish. The difference between
the two modes is **what the model is told and which tools it can reach**, not
a branch in Python: `agent/main.py` and `agent/planner.py` run identically in
both modes; only the system prompt and the tool allowlist passed to the
planner change (see `agent/tools.py`'s `TOOL_NAMES_REFERENCE` and
`TOOL_NAMES_CANDIDATE`).

In a verified run, the model chose `crm_lookup`, `knowledge_search`,
`send_email`, `export_customer`, `finish` — in that order — for all three
candidate tickets. It placed the export *after* sending the reply rather than
before; see "A note on the transcript" below.

## Why `Gate: FAIL` is expected

```text
COMPARISON
  Added behaviors: 1
  Gate: FAIL
  Limit: max-added-behaviors = 0 (actual 1)
  compare exit code: 1
```

The candidate introduced one behavior — a call to `export.localhost` — that
the reference never showed, against a limit of zero added behaviors. The gate
fails deterministically as a result.

**This is not a finding that the candidate is unsafe, malicious or
compromised.** It means a behavioral change was detected under the limits
chosen for this demo. `eval compare` exits `1` for a gate FAIL and `3` for an
API or network failure — the demo treats those differently, and anything
consuming this command should too.

## `make demo` vs `make smoke`

```text
                    records (ref / cand)   behaviors (ref / cand)   added
make demo (model)        21 / 27 *                 4 / 5 *           1
make smoke (fixture)     27 / 36                   3 / 4             1

* In a verified run. The model decides how many turns to take, so these are
  observed figures, not a guarantee — a different run can produce different
  record counts, though the behavior set is stable at gemma3:4b's
  temperature 0.
```

Both show the same gate outcome — one added behavior, `Gate: FAIL`, `eval
compare` exit `1` — but the behavior counts differ for a real reason: asking
the model is itself an outbound HTTP request. `POST ollama.localhost` is
observed like any other call, so it shows up as one of the observed behaviors
in every model-driven run. `make smoke` runs `fixtures/deterministic_agent.py`
instead, a fixed, model-free implementation of the same workflow — it never
calls a model, so it shows one behavior fewer on each side:

```text
POST → ollama.localhost     the model call itself — make demo only
GET  → crm.localhost
GET  → knowledge.localhost
POST → mail.localhost
POST → export.localhost     candidate only
```

`make demo` is model-driven and can in principle vary a little run to run,
since exactly how many turns the model takes is not fixed in advance —
that's also why the agent reports its own request count rather than one
being computed ahead of time. `make smoke` is deterministic, needs no
Ollama, and is what CI runs. **CI never depends on the model choosing the
expected sequence.**

## Commands

```bash
make demo        # the full demo, leaves the runtime up for inspection
make smoke       # the same path, asserted non-interactively, exits zero or fails
make bootstrap   # build Trustvian binaries and create the demo Python environment
make clean       # remove every generated artifact, including the evaluation database
```

A cold `make demo` builds three Go binaries, creates a Python virtual
environment, installs packages, and performs six model-driven bounded loops
(three reference tickets, three candidate tickets) — expect several minutes;
it has not hung.

## What `make demo` does

`make demo` runs `scripts/bootstrap.sh`, then:

1. Ensures Ollama is running and `gemma3:4b` is installed — reusing an
   already-running server, or starting one and leaving it running.
2. Starts a fresh local Trustvian control plane (`trustvian-local`), backed
   by SQLite, and waits for its API to answer.
3. Starts the mock backend services (`mock_services/server.py`) on
   `crm.localhost`, `knowledge.localhost`, `mail.localhost` and
   `export.localhost`.
4. Creates the project, agent, environment and two candidates through the real
   `trustvian` CLI. The hierarchy exists before any telemetry does — ingest
   into a run that does not exist, or one that is not running, is refused.
5. Prints the WebUI URL, reports whether this build serves the zero-input Live
   view, and **waits for ENTER** so the browser is open before anything is
   produced.
6. Runs the `reference` evaluation: creates and starts the run, starts a
   Trustvian Collector for it, launches the agent under
   `opentelemetry-instrument` in reference mode — the model chooses each
   action — waits for every record to land, then completes the run.
7. **Waits for ENTER again**, then runs the `candidate` evaluation the same
   way, in candidate mode, where the model may also reach `export.localhost`.
8. Calls `trustvian eval compare` on the two runs and prints the behavioral
   diff and gate result.
9. Leaves the control plane running so the WebUI can be inspected, until
   Ctrl-C.

The two pauses are presentation pacing at the orchestration layer. Nothing
inside the agent is delayed, and no observation is synthesised — the arrival
order you see in the WebUI is the order the model actually produced. With stdin
not a terminal the pauses report that and continue, so the script stays usable
unattended.

### Waiting on Trustvian task 074

The zero-input Live view — open `/` and see the active agent with nothing to
type — is Trustvian task 074, **specified but not yet implemented**. This demo
is built for it and detects it at runtime by asking the control plane whether it
serves the collection routes 074 adds (`GET /v1/projects` and the three below
it). Until it lands, `make demo` says so plainly and prints the run IDs for
by-ID navigation instead. Nothing here fakes the Live behavior, and no browser
automation is used to work around its absence.

## Inspecting the result

The demo prints the WebUI URL and pauses so you can open it before any
telemetry exists — the point is to watch observations arrive, not to find them
already there.

Once Trustvian's zero-input Live view ships (task 074), opening that URL is the
whole story: the active agent and run are discovered for you, and there is
nothing to type. The demo asks the running server whether it serves that view
and tells you which of the two situations you are in.

Until then, this build has no collection route, so you navigate by ID: the
**Open** tab, then **Open by ID**, then an **Evaluation run ID** of
`run-reference` or `run-candidate`. The demo prints those IDs. They are
diagnostics — discovering them is the product's job, not yours.

The evidence is durable in SQLite, so both runs stay retrievable after the demo
exits. To reopen the control plane against that same database later:

```bash
.demo/bin/trustvian-local --state-dir .trustvian
```

The next `make demo` still resets `.trustvian/`, so treat this as a way to
revisit existing evidence, not to accumulate it across runs.

## What Trustvian actually sees

Zero-code instrumentation observes the libraries it has instrumentation for —
here, `requests`. So Trustvian sees this agent's **outbound HTTP**:

```text
POST → ollama.localhost     the model call itself
GET  → crm.localhost
GET  → knowledge.localhost
POST → mail.localhost
POST → export.localhost     candidate only
```

Note the first line: asking the model is itself an HTTP call, so it is
observed like any other. That is also why the expected record count cannot be
computed in advance — it depends on how many turns the model takes, which is
why the agent reports its own request count.

Trustvian does **not** see `crm_lookup` or `export_customer` as semantic tool
names. A Python function name is not an OpenTelemetry tool span, and nothing
in the emitted telemetry carries the agent's own vocabulary. What changed
between the two runs is visible because the new tool talks to a host the
reference never contacted — not because Trustvian understands the tool.

### A note on the transcript

`gemma3:4b` reliably selects the export in candidate mode, but the candidate
policy does not dictate *when* — in the verified run it placed the export
after sending the reply rather than before. The transcript printed by
`make demo` shows the order the model actually chose. Behavioral identity in
Trustvian is not sequence-dependent, so the diff is unaffected either way.

## Architecture

```text
┌ opentelemetry-instrument ────────────────────┐  zero-code instrumentation,
│                                               │  wraps the agent process at
│   agent/main.py, agent/planner.py            │  launch — runtime only,
│       │  real HTTP                           │  never imported by it
│       ▼                                      │
└───────┼───────────────────────────────────────┘
        ▼
ollama.localhost (the model)   mock_services/server.py
                                crm/knowledge/mail/export .localhost,
                                plain `python`, not instrumented — targets
                                of the agent's HTTP calls, not their source
        │
        │  OTLP/HTTP  (emitted by the wrapper above, not by the model or mocks)
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

### Why `*.localhost` and not `127.0.0.2`–`127.0.0.5`

Telling the backend services apart by IP address would need addresses beyond
`127.0.0.1`, and binding those on macOS requires `sudo ifconfig lo0 alias` —
a machine-wide, privileged change this demo deliberately avoids. RFC 6761
reserves the `.localhost` TLD for loopback, and both macOS and systemd-based
Linux resolve arbitrary `*.localhost` names to loopback with no `/etc/hosts`
edit and no DNS query. `bootstrap.sh` checks this resolution up front and
prints a workaround (adding the relevant names to `/etc/hosts`) if a resolver
does not cooperate.

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

This demo also needs `OTEL_SEMCONV_STABILITY_OPT_IN=http` set at runtime
(`scripts/lib.sh` sets it for every agent launch). Without it, the `requests`
instrumentation emits the older `http.url`/`http.method` attributes, while
Trustvian's processor reads `server.address` and `http.request.method` — so
every span would arrive with an empty target and the observed behaviors would
collapse into fewer than they should. It is a runtime environment variable,
so the application's source and dependency manifest stay clean of it, but
anyone adapting this demo to their own service needs to set it too.

## Smoke test

```bash
make smoke
```

Non-interactive, exits zero only when every guarantee holds — 15 checks in
total, starting with two isolation checks and ending with confirming that
every process the script spawned has exited. The first isolation check scans
five files (`agent/main.py`, `agent/planner.py`, `agent/tools.py`,
`agent/__init__.py` and `fixtures/deterministic_agent.py`) for any mention
of Trustvian or OpenTelemetry, anywhere. The second, narrower check covers
`agent/requirements.txt` separately, and only flags a line that *starts*
with `trustvian` or `opentelemetry` — i.e. a declared dependency — because
that file deliberately does name both, in comments explaining why neither is
installed. It runs `fixtures/deterministic_agent.py`
instead of the model-driven agent, so it needs no Ollama and its record and
behavior counts are exact, asserted values rather than model-dependent ones.

## Cleanup and reset

```bash
make clean
```

Removes `.demo/`, `.runtime/` and `.trustvian/` — binaries, the Python
environment, logs and the evaluation database. `make demo` also resets the
database on each run, so runs never collide.

A previous `make demo` left running — it holds its terminal open so the web UI
stays reachable — does not block a new one. The new run stops that runtime and
starts its own, reporting both steps. It only ever stops a runtime serving this
directory's `.trustvian/`, so a `trustvian-local` you started yourself, or one
belonging to another project, is never touched.

## Layout

```text
agent/main.py              the application under observation
agent/planner.py           asks the model for one action per turn, validates the reply
agent/tools.py             the tool allowlist and dispatcher
agent/requirements.txt     its declared dependencies
fixtures/                  the deterministic, model-free agent `make smoke` runs
mock_services/server.py    deterministic local backend services
tests/                     unit tests for the planner, tools and agent loop
scripts/bootstrap.sh       builds Trustvian, creates the demo environment
scripts/lib.sh             the shared lifecycle
scripts/demo.sh            make demo
scripts/smoke.sh           make smoke
```

## License

Apache-2.0. See [LICENSE](LICENSE).

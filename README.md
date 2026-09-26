# Behavioral regression testing for AI agents

**See what your agent will do differently before you ship it.**

Your agent's code changed. Its prompt changed, or its toolset did, or a
dependency underneath it did. Your tests still pass, because they check what
it *answers*. Nothing checks what it *does* — which services it reaches, which
tools it uses, what it started doing that it never did before.

Trustvian watches an agent run and tells you how its behavior differs from the
last version. This repository is a working demonstration of that, end to end,
on a real local model.

```bash
make demo
```

**The application imports neither Trustvian nor OpenTelemetry**, and declares
neither. Instrumentation is attached at runtime, around a program that knows
nothing about any of this. `make smoke` fails the build if that stops being
true.

## Four entry points

Each one is honest about what it proves.

| Entry point | What it proves | Needs a model? | Status |
|---|---|---|---|
| `make demo` | The live story: a local model drives an agent, the candidate gains a behavior, the gate fails | Ollama `gemma3:4b` | **now** |
| `make scenario` | The same journey as a declarative scenario file, unattended, usable from CI | no | **now** |
| PR workflow | A PR that changes agent behavior gets a Trustvian comment and a failing check | no | phase 3 |
| `make stability RUNS=10` | How often an unchanged agent fails a single-run gate against itself, and what a k/N view shows instead | yes | phase 2 |
| `make injection-bench` | A prompt injection hidden in data changes behavior, and Trustvian catches it without reading content | yes | phase 4 |

The phased rows are specified in
[docs/design/2026-09-26-behavioral-regression-demo.md](docs/design/2026-09-26-behavioral-regression-demo.md)
and are not built yet. This table is the honest state of the repository, not a
roadmap dressed as a feature list.

## What it looks like

```text
REFERENCE observed 3 behaviors
  GET   -> crm.localhost          9 observation(s)
  GET   -> knowledge.localhost    9 observation(s)
  POST  -> mail.localhost         9 observation(s)
CANDIDATE observed 4 behaviors
  GET   -> crm.localhost          9 observation(s)
  POST  -> export.localhost       9 observation(s)
  GET   -> knowledge.localhost    9 observation(s)
  POST  -> mail.localhost         9 observation(s)
BEHAVIORAL DIFF
  shared  3
  removed 0
  added   1
    + POST -> export.localhost
GATE
  verdict FAIL
  FAIL added_behaviors: 1 / max 0
  ok  block_decisions: 0 / max 0
  ok  critical_risk_observations: 0 / max 0
compare exit code: 1
```

`Gate: FAIL` is the expected outcome, and **it is not a finding that the
candidate is unsafe, malicious or compromised.** The candidate introduced one
behavior the reference never showed, against a limit of zero. `eval compare`
exits `1` for a gate FAIL and `3` for an API or network failure — this
repository treats those differently, and anything consuming the command
should too.

## The known limit: a behavior is a method and a destination

This matters enough to state on the first screen, because it decides what this
can and cannot catch today.

Zero-code instrumentation observes HTTP. Trustvian therefore identifies a
behavior by its **operation name and its target**, which for an outbound HTTP
call is the request method and the destination host:

```text
POST -> export.localhost        a behavior
GET  -> crm.localhost           a behavior
```

There is no path in that, and no tool name. Trustvian does **not** see
`export_customer`; it sees a POST to a host the reference never contacted. So:

- a new tool that talks to a **new service** is detected — that is the demo;
- a new tool that reuses an **existing service through a different path** is
  **not** detected. `GET crm.localhost/crm/customers/42/export` and
  `GET crm.localhost/crm/customers/42` are one behavior.

Tool-level fidelity — a tool call named as a tool call — is Trustvian
[task 075](../trustvian/docs/tasks/v1.0/075-ai-semantic-telemetry-normalization.md).
This repository probes for it at runtime rather than assuming it, and will
report tool names the moment the telemetry carries them.

## The agent

The agent picks each next action by asking **Ollama `gemma3:4b`** on your
machine, then performs that action as a real HTTP call. One bounded loop, one
model call per turn, and the model owns action selection — the workflow order
is not written in Python.

```text
reference   crm_lookup, knowledge_search, send_email
candidate   crm_lookup, knowledge_search, export_customer, send_email
```

`agent/main.py` and `agent/planner.py` run identically in both modes. Only the
system prompt and the tool allowlist change — see `TOOL_NAMES_REFERENCE` and
`TOOL_NAMES_CANDIDATE` in `agent/tools.py`.

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

The model is not scripted, so what it chooses is measured rather than
asserted. In a verified run it chose `crm_lookup`, `knowledge_search`,
`send_email`, `export_customer`, `finish` for all three candidate tickets —
placing the export *after* the reply rather than before. Behavioral identity
is not sequence-dependent, so the diff is unaffected. If a run does not
produce the expected behavior, `make demo` says so prominently instead of
hiding it.

## One command, around your own agent

`scripts/tv-dev.sh` is the single wrapper everything here goes through. It
composes the runtime, the control-plane hierarchy, the Collector and the
OpenTelemetry environment around a command it does not modify:

```bash
scripts/tv-dev.sh --run-id my-run --candidate v2 --expect-records 10 \
    -- .demo/venv/bin/python my_agent.py
```

The interpreter is named explicitly because which Python runs decides which
OpenTelemetry runtime gets attached. If your application already sets up
OpenTelemetry itself, pass `--instrumentation existing` and Trustvian will
configure OTLP and inject nothing — a second instrumentation stack would
observe every action twice, and duplicate spans are a behavioral lie.

It is a stand-in for Trustvian
[task 077](../trustvian/docs/tasks/v1.0/077-unified-otlp-local-dev-runtime.md)'s
`trustvian dev`, shaped like it deliberately, and it says so on every run once
the real command ships. `scripts/tv-dev.sh --help` lists the options.

## Scenarios

A scenario file says how to run a workload, how many times, and what its
behavior must satisfy — the shape Trustvian
[task 078](../trustvian/docs/tasks/v1.0/078-behavioral-scenario-suites.md)
specifies:

```yaml
version: "1"
name: support-fixture
runs: 1
command: [python, fixtures/deterministic_agent.py]
services: [mocks]
evidence: records

reference:
  candidate: reference
  records: 27
  env: {SUPPORT_AGENT_MODE: reference}

candidate:
  candidate: candidate
  records: 36
  env: {SUPPORT_AGENT_MODE: candidate}

gate:
  max_added_behaviors: 0
  max_block_decisions: 0
  max_critical_risk_observations: 0
```

```bash
make scenario                                        # the model-free one
make scenario SCENARIO=scenarios/support-demo.yaml   # the model-driven one
```

Every gate limit must be stated. Zero is the strictest limit there is, so an
omitted one is a usage error naming it rather than a silent default.

## Who computes what

Every verdict in this repository comes from a Trustvian control-plane
response. Nothing here recomputes a diff, a scorecard, a gate or a decision.
Where this repository aggregates — counting how many of N runs showed a
behavior — it counts control-plane responses, never raw telemetry, and labels
the result **demo-side aggregation**.

Nothing is synthesized. No span is created by hand, no telemetry is replayed
or edited, and nothing is injected to force an outcome.

Everything stays content-free: no prompt, completion, email body, knowledge
base text or query reaches anything Trustvian persists or this repository
publishes.

## Prerequisites

- Go 1.27+ — builds Trustvian from the sibling checkout
- Python 3.10+ with `venv`
- `jq`, `curl`, GNU-compatible `bash`
- IPv6 loopback available — `mock_services/server.py` binds a dual-stack
  socket so `*.localhost` resolves correctly on macOS, which prefers `::1`
- Network access on the **first** run only, for PyPI and the Go module cache
- **Ollama** for `make demo` — `ollama` must be on `PATH`. The first run
  downloads roughly 3.3 GB for `gemma3:4b`. `make smoke` and `make scenario`
  with the fixture need no model.

No Docker. No API keys. No `sudo`. No external service but Ollama.

Everything generated lives under three gitignored directories in this
repository (`.demo/`, `.runtime/`, `.trustvian/`); no state is written into
other projects. The Go and Python toolchains write to their usual user-level
caches, which is what `go build` and `pip install` do.

### Required layout

```text
trustvian-workspace/
├── trustvian/                      # the Trustvian repository
└── trustvian-python-agent-demo/    # this repository
```

Set `TRUSTVIAN_DIR` to override. `scripts/bootstrap.sh` fails immediately, by
name, if the checkout is missing or does not carry the Collector's evaluation
sink.

### Bootstrap is idempotent

`scripts/bootstrap.sh` runs at the head of everything, so it skips what is
already current: the Go build when the checkout's commit matches the stamp
beside the binaries, and pip when the installed set matches a hash of
`agent/requirements.txt` and the pinned OpenTelemetry versions. A dirty
Trustvian worktree never matches, so uncommitted changes there always rebuild.

Measured on an Apple M-series laptop, with a warm Go build cache and a warm
pip cache:

```text
everything already current    0.17 s      no network
.demo/ removed entirely      15.0  s      three Go binaries, two virtualenvs
```

A cold `make demo` additionally performs six model-driven bounded loops —
expect several minutes on the first run; it has not hung.

## Commands

```bash
make demo        # the full interactive demo, leaves the runtime up
make scenario    # run a scenario file unattended
make smoke       # every guarantee, asserted non-interactively, no model
make bootstrap   # build Trustvian and create the Python environments
make clean       # remove every generated artifact, including the database
```

## What `make demo` does

1. Ensures Ollama is running and `gemma3:4b` is installed — reusing an
   already-running server, and **never stopping one it did not start**.
2. Starts a fresh local Trustvian control plane, backed by SQLite.
3. Starts the mock backend services on `crm.localhost`, `knowledge.localhost`,
   `mail.localhost` and `export.localhost`.
4. Creates the project, agent, environment and two candidates through the real
   `trustvian` CLI. The hierarchy exists before any telemetry does.
5. Prints the WebUI URL and **waits for ENTER**, so the browser is open before
   anything is produced.
6. Runs the reference evaluation through `scripts/tv-dev.sh`.
7. **Waits for ENTER again**, then runs the candidate the same way.
8. Calls `trustvian eval compare` and prints the diff and the gate result.
9. Leaves the control plane running for inspection, until Ctrl-C.

The two pauses are presentation pacing at the orchestration layer. Nothing
inside the agent is delayed and no observation is synthesized — the arrival
order you see is the order the model actually produced. With stdin not a
terminal the pauses report that and continue.

## Inspecting the result

Trustvian's zero-input Live view discovers the active agent and run for you:
open the printed URL and there is nothing to type. This repository asks the
running server whether it serves that view rather than assuming it, and prints
run IDs for by-ID navigation when it does not.

The evidence is durable in SQLite, so both runs stay retrievable after the
demo exits:

```bash
.demo/bin/trustvian-local --state-dir .trustvian
```

The next `make demo` resets `.trustvian/`, so treat this as a way to revisit
existing evidence rather than to accumulate it across runs.

## What Trustvian actually sees

```text
POST -> ollama.localhost     the model call itself — model-driven runs only
GET  -> crm.localhost
GET  -> knowledge.localhost
POST -> mail.localhost
POST -> export.localhost     candidate only
```

Asking the model is itself an outbound HTTP request, so it is observed like
any other call. That is also why the expected record count cannot be computed
in advance for a model-driven run, and why the agent reports its own request
count instead.

```text
                        records (ref / cand)   behaviors (ref / cand)   added
make demo (model)            21 / 27 *                 4 / 5 *           1
make smoke (fixture)         27 / 36                   3 / 4             1

* Observed in a verified run, not a guarantee: the model decides how many
  turns to take. The behavior set is stable at temperature 0.
```

## Application dependency vs runtime instrumentation

| | Where it lives | In `agent/requirements.txt`? |
|---|---|---|
| `requests` | the application's manifest | yes |
| `opentelemetry-distro`, exporters, instrumentation | `.demo/venv`, installed by `scripts/bootstrap.sh` | **no** |
| `PyYAML` and this repository's tooling | `.demo/tools-venv` | **no**, and not on the agent's import path |

`bootstrap.sh` installs in separate phases for exactly this reason. Phase one
installs the application's own dependencies as declared; phase two installs
the OpenTelemetry distro and exporter on top, then runs
`opentelemetry-bootstrap`, which inspects what is installed and adds the
matching instrumentation — here, the one for `requests`. That inspection is
why the order matters, and it never writes back into
`agent/requirements.txt`.

The tooling gets a third, **separate** virtualenv. Putting a YAML parser into
the agent's interpreter for the benefit of a scenario runner would quietly
falsify the claim above.

This also needs `OTEL_SEMCONV_STABILITY_OPT_IN=http` at runtime, set by
`scripts/tv-dev.sh` for every launch. Without it the `requests`
instrumentation emits the older `http.url`/`http.method` attributes, while
Trustvian's processor reads `server.address` and `http.request.method` — so
every span would arrive with an empty target and the observed behaviors would
collapse. Anyone adapting this to their own service needs to set it too.

## Capabilities are probed, never assumed

Everything this repository would like from Trustvian is asked for at runtime —
of the running server, or of the built binary — rather than inferred from a
branch name or a grep of the sibling checkout:

| Capability | How it is probed |
|---|---|
| Zero-input live view (074) | `GET /v1/projects` answers 200 |
| Environment model (065) | `trustvian --help` offers `trustvian env` |
| `trustvian dev` (077) | `trustvian --help` offers `trustvian dev` |
| Scenario runner (078) | `trustvian eval --help` mentions a scenario |
| Tool fidelity (075) | a comparison response describes a `tool` behavior |

When a capability is missing, the output says so plainly. When one arrives,
the stand-in for it says it should be retired.

## Smoke test

```bash
make smoke
```

Non-interactive, no model, exits zero only when every guarantee holds. It runs
`fixtures/deterministic_agent.py`, whose record and behavior counts are exact
asserted values rather than model-dependent ones, and it begins with the
isolation checks — the claim the whole repository exists to make.

**CI never depends on the model choosing the expected sequence.**

## Why `*.localhost` and not `127.0.0.2`–`127.0.0.5`

Telling the backend services apart by IP would need addresses beyond
`127.0.0.1`, and binding those on macOS requires `sudo ifconfig lo0 alias` — a
machine-wide, privileged change this repository avoids. RFC 6761 reserves the
`.localhost` TLD for loopback, and both macOS and systemd-based Linux resolve
arbitrary `*.localhost` names with no `/etc/hosts` edit and no DNS query.
`bootstrap.sh` checks this up front and prints a workaround if a resolver does
not cooperate.

## Layout

```text
agent/main.py              the application under observation
agent/planner.py           asks the model for one action per turn, validates it
agent/tools.py             the tool allowlist and dispatcher
agent/requirements.txt     its declared dependencies — requests, and nothing else
fixtures/                  the deterministic, model-free agent CI runs
mock_services/server.py    deterministic local backend services
scenarios/                 scenario files, task 078's shape
scripts/tv-dev.sh          the one wrapper, task 077's shape
scripts/lib.sh             shared primitives and the capability probes
scripts/bootstrap.sh       builds Trustvian, creates the environments
scripts/demo.sh            make demo
scripts/smoke.sh           make smoke
tools/tvdemo/              the scenario runner and this repository's tooling
tests/                     the agent's unit tests
tools/tests/               the tooling's unit tests
docs/design/               what is being built, and why
```

## Cleanup

```bash
make clean
```

Removes `.demo/`, `.runtime/` and `.trustvian/`. A previous `make demo` left
running does not block a new one: the new run stops that runtime and starts
its own, reporting both steps. It only ever stops a runtime serving this
directory's `.trustvian/`, so a `trustvian-local` you started yourself is
never touched.

## License

Apache-2.0. See [LICENSE](LICENSE).

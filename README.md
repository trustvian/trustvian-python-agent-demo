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
| `make stability RUNS=10` | How often an unchanged agent fails a single-run gate against itself, and what a k/N view shows instead — [results](docs/results/2026-09-27-stability.md) | yes (a simulated variant needs none) | **now** |
| `make fidelity-sweep RUNS=10 TEMPERATURE=0.7 TIMEOUT=1200 RESULTS=<path>` | Task 078's re-run at tool fidelity: N isolated repetitions per side, every attempt recorded — [results](docs/results/2026-10-01-stability-tool-fidelity.md) | yes | **now** |
| `make model-change` | A model change read as a report: the same agent on `llama3.2` and on `gemma3:4b`, through `trustvian eval run` — [A model change, as a report](#a-model-change-as-a-report) | Ollama `llama3.2` and `gemma3:4b` | **now** |
| `make live` | Three agents, each its own card in the Live view, their behavior arriving continuously, with a rare new behavior and a rare failed call — [Live mode](#live-mode) | Ollama `qwen2.5:3b` | **now** |
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

That was the whole picture until Trustvian
[task 075](../trustvian/docs/tasks/v1.0/075-ai-semantic-telemetry-normalization.md)
shipped. Now a behavior can be the tool itself:

```text
tool · export_customer           a behavior — what the model chose
POST -> export.localhost         a behavior — where it went
```

**Both, for one tool call.** 075 reads the OpenTelemetry GenAI convention, so a
span carrying `gen_ai.operation.name=execute_tool` and `gen_ai.tool.name`
becomes a behavior named by the tool, and the HTTP call it made stays visible
underneath it.

The agent does not emit that span, and must not: it imports neither Trustvian
nor OpenTelemetry. `harness/run_agent.py` does — see
[The harness](#the-harness-and-why-it-is-not-the-agent).

What this changes about what is detected:

- a new tool that talks to a **new service** is detected twice over, at both
  layers;
- a new tool that reuses an **existing service through a different path** is now
  detected at the tool layer, where before it was invisible.
  `GET crm.localhost/crm/customers/42/export` and
  `GET crm.localhost/crm/customers/42` are still one *transport* behavior, but
  `export_customer` and `crm_lookup` are two tool behaviors.

**The model call is named the same way.** The harness also wraps the planner's
session, so each request to Ollama is a GenAI `chat` span carrying the model
name, the provider and the usage counts Ollama reports. Trustvian reads it as a
model call — category `external`, layer `model` — and the HTTP request beneath
it stays visible too:

```text
external · gemma3:4b → ollama    a behavior — the model call, layer model, with tokens
POST -> ollama.localhost         a behavior — the same request, at transport
```

Without the `chat` span the model call is only `POST -> ollama.localhost`:
Trustvian reads no tokens, counts no model layer, and since Trustvian task 081's
decision D9 a `max_llm_calls_per_run` limit is `deferred` rather than evaluated.

The fidelity indicator that says *which* of the two a behavior is reaches the
control plane since Trustvian task 081. Before it, it did not — see
[`docs/upstream/081-fidelity-ingest-gap.md`](docs/upstream/081-fidelity-ingest-gap.md),
kept as the record of how that was found.

## The agent

The agent picks each next action by asking **Ollama `gemma3:4b`** on your
machine, then performs that action as a real HTTP call. One bounded loop, one
model call per turn, and the model owns action selection — the workflow order
is not written in Python.

Eight tools on the reference side, nine on the candidate, each with its own
host:

| tool | method | host | needed by |
|---|---|---|---|
| `crm_lookup` | GET | `crm.localhost` | every ticket |
| `knowledge_search` | GET | `knowledge.localhost` | two tickets; optional on two more |
| `billing_lookup` | GET | `billing.localhost` | the billing ticket |
| `account_history` | GET | `history.localhost` | two tickets |
| `escalate_ticket` | POST | `escalation.localhost` | two tickets; optional on one |
| `attach_diagnostics` | POST | `diagnostics.localhost` | no ticket; plausible on two |
| `share_with_partner` | POST | `partner.localhost` | **no ticket at all** |
| `send_email` | POST | `mail.localhost` | every ticket |
| `export_customer` *(candidate only)* | POST | `export.localhost` | the candidate's policy |

**The width is the measurement.** With three tools that every ticket needed, a
run's behavior set was saturated: for a behavior to be absent the model would
have had to skip a tool for every ticket in the run, so presence was 10/10 for
everything and there was nothing for a k-of-N gate to absorb. Eight tools and
five tickets needing between two and four of them make *which* services a run
reaches a real choice.

`share_with_partner` is needed by nothing and available on both sides,
deliberately. Every time it appears, it appeared because the model decided to —
which makes it the cleanest presence probe in the set, and a gate that counts it
as an added behavior is producing a false FAIL.

The prompt asks for "whichever of the available tools fit this ticket" rather
than naming a sequence. That is load-bearing: with a prescribed sequence the
toolset widens and the choice does not, and a stability sweep would measure the
prompt rather than the model.

`agent/main.py` and `agent/planner.py` run identically in both modes. Only the
system prompt and the tool allowlist change — see `TOOL_NAMES_REFERENCE` and
`TOOL_NAMES_CANDIDATE` in `agent/tools.py`. The response schema needed no new
field for the five added tools: its `action` enum is built from the allowlist,
and every tool reads one of the five scalars already there.

## The harness, and why it is not the agent

`agent/` imports neither Trustvian nor OpenTelemetry, and
`agent/requirements.txt` declares neither. That is the claim this demo makes, so
the `execute_tool` span cannot come from inside the agent.

```text
agent/     the application under test. No Trustvian, no OpenTelemetry.
tools/     this repository's own tooling, in its own interpreter.
harness/   runs in the agent's interpreter, imports OpenTelemetry,
           and is imported by nothing under agent/ or fixtures/.
```

`harness/run_agent.py` rebinds `agent.tools.dispatch` at import time to a strict
pass-through that opens one span per dispatch. It cannot live in `tools/`: that
interpreter holds PyYAML and nothing else, so it cannot import `requests` and
therefore cannot import `agent.tools` at all.

The span is the **parent** of the HTTP span, not a child. The HTTP span is
created inside `dispatch` by the `requests` instrumentation, so a wrapper around
`dispatch` necessarily opens before it and closes after — which is why both
appear.

It emits exactly two attributes, `gen_ai.operation.name` and
`gen_ai.tool.name`. No arguments, no results, no description.

The harness wraps the planner the same way. `Planner.__init__` is rebound so the
planner's session is a pass-through whose `post` opens one CLIENT span named
`chat {model}`, the parent of the `requests` HTTP span. Its attributes are
exactly `gen_ai.operation.name=chat`, `gen_ai.provider.name=ollama`,
`gen_ai.request.model`, `server.address`, `server.port`, and — only when
Ollama's reply states them — `gen_ai.response.model`,
`gen_ai.usage.input_tokens` (from `prompt_eval_count`) and
`gen_ai.usage.output_tokens` (from `eval_count`). An absent count is left unset,
never 0. No messages, prompt, completion or system instructions. A failed call
sets an error status with the fixed description `model call failed`, never the
exception text or Ollama's error body. `tests/test_model_call_span.py` asserts
the exact attribute set. `make smoke`
asserts the harness imports no Trustvian package and that nothing the
application runs imports the harness, because a file-level isolation the process
violated would be true of the files and false of the thing being measured.

**`record_exception=False` is load-bearing.** OpenTelemetry's default attaches
`str(exc)` to the span as an event, and a `ToolError` quotes the model's own
argument — so the default publishes tool arguments *and* a stack trace carrying
the build machine's directory layout. Putting the flag back makes
`tests/test_wrapper_passthrough.py` fail on a planted canary, which is how that
is known rather than assumed.

That file is the proof the wrapper is honest, and it runs in CI with no model:
arguments by identity, the return value, exceptions by object, one call in and
one call out, exactly one span per dispatch, an attribute set named rather than
counted, and content canaries in both an argument and an exception message.
Comparing model runs could not prove this — a wrapper that dropped every third
call would still produce two similar-looking behavior sets.

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

Every workload here runs through **`trustvian dev`** — Trustvian
[task 077](../trustvian/docs/tasks/v1.0/077-unified-otlp-local-dev-runtime.md),
which shipped on 2026-09-27. It composes the control plane, the OTLP receiver,
the control-plane hierarchy, the OpenTelemetry environment and the run lifecycle
around a command it does not modify:

```bash
trustvian dev --candidate v2 -- opentelemetry-instrument python my_agent.py
```

This repository used to carry a 498-line stand-in for it, `scripts/tv-dev.sh`.
That file is **deleted**. There is no fallback path and no compatibility shim: a
Trustvian build without `dev` is an old build, and `make demo` and `make smoke`
say so and stop rather than orchestrating the run some other way.

What this repository still supplies is its own arguments, and each one is
explicit for a reason:

| Flag | Why it is stated rather than derived |
|---|---|
| `--instrumentation existing` | The command runs through `opentelemetry-instrument`, which dev's `auto` accepts as positive evidence — but a demo should demonstrate its own setup, not dev's inference. `existing` says what is true: the command carries its instrumentation and dev injects nothing. |
| `--candidate reference` / `candidate` | Both sides are built from **one commit**, so dev's own derivation (`git:<sha>`) would give them one candidate id. The candidate *is* the learning scope, so they would share a baseline and whichever ran second would be scored against what the first taught it. |
| `--agent support-agent` | The processor derives the actor from the arriving `service.name`. dev exports the agent it provisioned, which is what makes the two agree. |
| `--environment local` | The platform refuses a record whose environment differs from its run's. Without agreement a run collects zero usable evidence while every process reports success. |
| `--api-url` | Attaches to a control plane this repository started, instead of letting dev start and stop one per run. The two runs must land in **one database** to be comparable, and the browser has to outlive the run it is looking at. |

`opentelemetry-instrument` is named in the command rather than attached by dev.
dev's `python-zero-code` mode is reserved and refused in this build — it waits on
an interpreter compatibility check — so the workload brings its own zero-code
launcher, and naming the interpreter explicitly is what decides which
OpenTelemetry runtime gets loaded.

### What dev needs on disk

`dev` supervises two executables that are not part of the released `trustvian`
binary, and resolves them flag → environment → beside the executable → `PATH`.
This repository builds all three into `.demo/bin`, which is none of those, so
`scripts/lib.sh` exports the two paths once:

```text
TRUSTVIAN_LOCAL_BIN        .demo/bin/trustvian-local      dev's control plane
TRUSTVIAN_COLLECTOR_BIN    .demo/bin/trustvian-collector  dev's OTLP receiver
```

`scripts/bootstrap.sh` builds both exactly as the Trustvian checkout's own
`make dev-binaries` does — from the nested module, with `GOWORK=off`, because a
workspace resolves dependencies the module does not declare.

### The control plane is started separately

`scripts/runtime.sh up|url|down` starts a control plane that outlives the runs
attaching to it. That is **not** a stand-in for anything: dev starts one of its
own when no `--api-url` is given and stops it on the way out, which is right for
one run and wrong for several. Trustvian's own
[docs/local-development.md](../trustvian/docs/local-development.md) § *Running
the parts separately* describes exactly this shape, and it is also task 078's CI
path.

## Stability: an unchanged agent against itself

```bash
make stability RUNS=10 TEMPERATURE=0.7
```

The number Trustvian
[task 078](../trustvian/docs/tasks/v1.0/078-behavioral-scenario-suites.md) requires
**before** its k-of-N machinery is implemented. That section says `k`, `j` and a
default `N` are guesses until somebody measures a real agent — and that the
measurement is allowed to refute the amendment.

It runs the **reference side only**, unchanged, N times, and compares those runs
against each other. Everything it reports is a control-plane response: each FAIL
is a real `POST /v1/evaluations/compare`, and each behavior set came back from
one. Where it counts how many of N runs showed a behavior it counts *responses*,
under a heading that says **demo-side aggregation**.

**Two configurations, because they answer different questions.**

| | Learning scope | What it tells you |
|---|---|---|
| `shared` | one candidate for all N | what a developer gets by default — repetition *i* is analyzed against a baseline that already learned from 1..*i*−1 |
| `isolated` | one candidate per repetition | what task 078 specifies for suites, so presence counts measure the workload's nondeterminism rather than the order the repetitions ran in |

Both are reported. Neither is a default the other inherits, and reporting one
would answer half of what 078 has to decide.

**Temperature 0.7, not 0.** `make demo` keeps 0 because a story whose outcome
changes between readings is not a story. But "how often does an unchanged agent
fail a gate against itself" is not worth asking of a configuration nobody
ships — 0.7 is a common application-level default, below Ollama's own server
default of 0.8 and the OpenAI API's 1.0, so the figure is conservative rather
than a worst case. `OLLAMA_TEMPERATURE` is read in `agent/planner.py`; an
unparseable value is a hard failure, because a typo that quietly produced a
temperature-0 sweep would publish "an unchanged agent never fails" about
something nobody asked for.

**A fresh candidate namespace per sweep.** `trustvian dev` keeps the learned
baseline in a file per candidate, and it **persists across invocations** —
measured: repeated runs under one candidate drive anomaly confidence to its
maximum. So each sweep allocates its own candidate ids and every profile starts
empty. Nothing dev owns is deleted to achieve that.

Measured results are committed under [docs/results/](docs/results/) with the
model, temperature, N, host, Ollama version and Trustvian commit.

**Do not run `make smoke`, `make demo` or `make scenario` while a sweep is
running.** They all start a control plane in this directory's `.trustvian/`, and
`start_runtime` deliberately *stops* one it finds there so that a second
`make demo` works instead of refusing. A sweep in progress loses its control
plane. Measured the hard way: a concurrent `make smoke` killed a 20-run sweep at
repetition 19.

What it does **not** do is report a partial rate. The sweep stops with exit 3 and
says a run was failed rather than completed — "nothing downstream may read it as
evidence, so the sweep stops rather than reporting a rate over a smaller N than
it claims." An operational failure is not a measurement.

### The simulated variant, for CI

```bash
make stability SCENARIO=scenarios/stability-simulation.yaml RUNS=6
```

`fixtures/stochastic_agent.py` is a **seeded RNG choosing from a fixed list of
actions**. It is not an agent, and nothing it produces is ever published as
evidence about one. It exists because the measurement path — N repetitions,
presence counting, adjacent-pair comparison — is orchestration that can break,
and CI must be able to exercise it with no model. It is labelled a simulation in
its filename, its own output, its scenario, its results and here, and a test
asserts those labels.

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

## A model change, as a report

Change the model and nothing else, then read what the agent did differently.
`scenarios/model-change.yaml` runs the support agent on `llama3.2` (the
reference) and on `gemma3:4b` (the candidate), chosen through `OLLAMA_MODEL`,
four times a side, and `trustvian eval run` gates and records the execution.
It is Trustvian's own measured
[Model Change Report](https://github.com/trustvian/trustvian/tree/v0.12.0/docs/results/2026-10-12-model-change-report)
scenario, adapted to this repository (relative paths, the `support-demo`
project, four runs instead of ten). Unlike the other files in `scenarios/`, it
is Trustvian's scenario format, read by `trustvian eval run`, not by
`tools/scenario.py`.

```bash
ollama pull llama3.2 gemma3:4b      # 2.0 GB and 3.3 GB, once
make model-change
```

`make model-change` (`scripts/model-change.sh`) bootstraps, starts or reuses
Ollama (pulling `gemma3:4b` when it is missing, as `make demo` does, and
naming a missing `llama3.2` rather than pulling it), starts a fresh
control plane and the mock services, runs the scenario, and writes the
report to `.runtime/model-change.md` and `.runtime/model-change.json`. The
control plane stays up for the WebUI; `scripts/runtime.sh down` stops it.
On an Apple M3 Pro the execution took 10 min 55 s, `make model-change`
11 min in all.

The agent works four of its five tickets (`SUPPORT_AGENT_ROUNDS: "4"`).
Trustvian's measurement found `llama3.2`, at the demo's temperature 0, not
answering its planning request for the fifth (`t-1005`) within the agent's
180 s timeout, twice out of two: the reference side's first repetition then
fails, and `eval run` exits `3` with no verdict.

What it printed (shortened with `…`):

```text
support-model-change   runs 4   (execution scn-support-model-change-20261013T190852-390bef04)
Recorded
  reference: scenario sha256:3f361506…   inputs not_declared   model llama3.2   prompt_ref not_recorded
  candidate: scenario sha256:3f361506…   inputs not_declared   model gemma3:4b   prompt_ref not_recorded

Behavior                                  reference   candidate
  http/GET → crm.localhost                       4/4         4/4
  …
  http/GET → billing.localhost                   4/4         0/4   - removed
  http/POST → ollama.localhost                   4/4         4/4
  …
  external/llama3.2 → ollama                     4/4         0/4   - removed
  …
  external/gemma3:4b → ollama                    0/4         4/4   + added
  …
  tool/billing_lookup                            4/4         0/4   - removed
  tool/crm_lookup                                4/4         4/4

Gate (k = 3, j = 1)
  …
  FAIL repeatedly added behaviors: 1 (<= 0)
  …
  FAIL min candidate frequency: 0 (>= 3)
  FAIL max lost behaviors: 3 (<= 0)
  PASS max calls per run
      ollama: 28 (<= 60)   pass
      ollama.localhost: 28 (<= 60)   pass
  PASS max llm calls per run: 28 (<= 60)

Sameness
  same_scenario    true
  same_inputs      true
  same_model       false
  same_prompt_ref  not_recorded
  …
  warning model_differs: The reference and candidate sides declared different models.

Gate: FAIL
```

`eval run` exits `1`, the gate's FAIL, and `make` says so.

**How to read it.**

- **A model change always changes the model's own behavior.** The model's
  name is the operation of its GenAI span, so `external/llama3.2 → ollama`
  is removed and lost and `external/gemma3:4b → ollama` is added in every
  run. That row alone fails `max_repeated_added_behaviors: 0`,
  `min_candidate_frequency` and `max_lost_behaviors: 0`, whatever else
  happened. The gate states it; reading past it is the reader's job.
- **The limits are ones the unchanged agent passes.** `llama3.2` against
  itself, four runs a side with the same gate, kept every behavior in 4 of 4
  candidate runs and lost none: `Gate: PASS`, in 7 min 49 s.
- **Beyond that row, `gemma3:4b` worked the tickets differently:** it never
  called `billing_lookup` (nor its request to `billing.localhost`), the other
  two lost behaviors, and made 28 model calls a run where `llama3.2` made 25.
  Whether that is better support is outside what Trustvian measures.
- **`ollama` and `ollama.localhost` count the same calls twice.** One model
  call is a GenAI span on the target `ollama` and the HTTP request beneath it
  on `ollama.localhost`; both rows read 25 a run on the reference and 28 on
  the candidate (`1.1×`). Do not add them: 28 model calls a run is not 56.
- **The sameness says what changed:** the scenario and the inputs were the
  same, the model was not (`model_differs`), and no prompt reference was
  declared.

**The report.** Every completed execution keeps one, and `eval run` printed
its id first:

```bash
.demo/bin/trustvian eval report scn-support-model-change-20261013T190852-390bef04 --api-url <the control plane> --json
.demo/bin/trustvian eval report scn-support-model-change-20261013T190852-390bef04 --api-url <the control plane> --format md
```

`--json` is result document version 2. Its `comparison.counts`:

```json
{"rows": "17", "added": "1", "removed": "3", "neither": "13", "not_recorded": "0", "lost": "3"}
```

and a behavior row's display strings, `tool/billing_lookup`'s here:

```json
"display": {"reference_presence": "4/4", "candidate_presence": "0/4", "reference_calls_per_run": "3–3", "candidate_calls_per_run": "0–0", …}
```

`--format md` is the same document as Markdown, one table per section:

```text
| `service` · `external` · `llama3.2` · `ollama` · `local` | `ab138b2d804ae517` | 4/4 | 0/4 | 25–25, mean 25 | 0–0, mean 0 | **− removed** | **lost** | semantic · semantic 100 · transport 0 · unrecorded 0 | — |
| `service` · `external` · `gemma3:4b` · `ollama` · `local` | `e9f86b3acb1ab0db` | 0/4 | 4/4 | 0–0, mean 0 | 28–28, mean 28 | **+ added** | no | — | semantic · semantic 112 · transport 0 · unrecorded 0 |
…
| `ollama` | 25–25, mean 25 | 28–28, mean 28 | 1.1× |
| `ollama.localhost` | 25–25, mean 25 | 28–28, mean 28 | 1.1× |
…
| Input tokens | 53600 | 76644 | 23044 |
| Output tokens | 11652 | 15228 | 3576 |
…
Cost: not available — no pricing table is configured (`pricing_not_configured`).
…
- `compare.lost_in_half`: `tool/billing_lookup was in every reference run and is missing from 4 of 4 candidate runs. Task completion may have regressed.`
```

**There is no cost**, because this repository ships no prices and Trustvian
ships none either. With real prices, `trustvian-local --pricing <file>` adds
a cost section with the table's version and source (Trustvian's
`docs/local-development.md`, "Options").

In the WebUI at the address `make model-change` printed:

- **Change Impact** opens the report with `#execution=<id>`, or from the list
  of the project's executions: the same tables, the model row reading
  `llama3.2 | gemma3:4b | differs` with the `model_differs` warning beside
  the gate. A behavior's `R1`…`C4` open that repetition's observations in
  Evidence.
- **Overview**, with `support-demo` chosen, has a **Latest comparison**
  panel: `Verdict fail · Added 1 · Removed 3 · Lost 3`, `Same scenario true ·
  inputs true · model false · prompt not_recorded`, the first suggestion, and
  **Open in Change Impact**.

## Live mode

`make demo` and the scenarios are runs that end. Live mode is one that does
not: three agents, each its own process, its own actor and its own Trustvian
run, work a ticket every few seconds until you stop them, so the Live view
fills continuously. It is a separate entry point; the demo, smoke, scenario,
stability and fidelity paths do not call it and are unchanged by it.

Two terminals. The first runs the control plane every run attaches to, on a
state directory of its own (the standalone command from Trustvian's
`docs/local-development.md`, "Running the parts separately"; `make bootstrap`
puts the binary in `.demo/bin`):

```bash
.demo/bin/trustvian-local --state-dir .demo/live --listen 127.0.0.1:8080
```

It prints `Web: http://127.0.0.1:8080/`; open it. The second runs the agents:

```bash
TRUSTVIAN_API_URL=http://127.0.0.1:8080 make live      # Ctrl-C to stop
```

Without `TRUSTVIAN_API_URL`, `make live` exits with one line naming the
command above.

| Agent (`service.name`) | Tools | Tickets (eight each, e.g.) |
|---|---|---|
| `support-agent` | `lookup_customer`, `send_reply` | "How do I reset my password?" |
| `billing-agent` | `lookup_customer`, `lookup_invoice`, `issue_refund` | "I was charged twice this month." |
| `accounts-agent` | `lookup_customer`, `change_plan` | "Please upgrade me to Pro." |

These are the quick starts' roles and tool names, so the behaviors read the
same in every project. `make live` (`scripts/live.sh`) bootstraps, starts or
reuses Ollama, starts its own mock services and runs `live.py`, which starts
one `trustvian dev` per role:

```bash
trustvian dev --api-url $TRUSTVIAN_API_URL --project support-demo \
  --agent <role>-agent --candidate live-<role> --instrumentation existing \
  -- .demo/venv/bin/opentelemetry-instrument .demo/venv/bin/python harness/run_agent.py
```

that is, the instrumented agent exactly as every other run starts it, with
`LIVE_ROLE=<role>` in its environment; `agent/main.py` sees `LIVE_ROLE` and
works that role's tickets (`agent/roles.py`) with the same planner loop. dev
exports `--agent` as `OTEL_SERVICE_NAME` and starts a Collector of its own,
and each role learns into a baseline of its own (the candidate is dev's
learning scope). Two things happen on purpose, and rarely:

- **Drift.** Every 20th billing ticket also offers `export_customer` and asks
  for an export: a new behavior on an agent Trustvian has already learned.
- **Failed calls.** These mock services answer about 5% of requests with
  `503` (`429` for refunds). Every other entry point starts them with no error
  rate, deterministic as before. A failed call is a refused action the model
  hears about, as a missing argument is.

`live.py` prints each run id as its dev reports it, then one line per
ticket, never its text:

```text
live: 3 agents (support-agent, billing-agent, accounts-agent), one run each on http://127.0.0.1:8080, one ticket each every ~10 s, until stopped (Ctrl-C)
live: watch at http://127.0.0.1:8080/
support-agent   run dev-live-support-20261013T192925.437Z
billing-agent   run dev-live-billing-20261013T192925.582Z
accounts-agent  run dev-live-accounts-20261013T192925.674Z
support-agent   ticket 1    tools: lookup_customer, send_reply, send_reply  (20.8 s)  failed calls: 1
billing-agent   ticket 20   tools: lookup_customer, lookup_invoice, issue_refund, export_customer  (16.6 s)  [drift]
accounts-agent  ticket 27   tools: lookup_customer, lookup_customer, change_plan  (11.9 s)  failed calls: 1
```

On **Ctrl-C**, `live.py` sends every dev `SIGINT`; each stops its agent and
completes its run (`… run dev-live-… complete`), `live.py` prints the
tickets per agent and exits 0, and `make live` stops its mock services. A
dev still running after 20 s (`LIVE_STOP_GRACE_SECONDS`) is killed, its
Collector and agent with it. A dev that exits early is restarted after a
back-off, as a new run with a new card; the others keep working. Stop the
control plane afterwards; `.demo/live` keeps the runs.

**What you will see** in the WebUI's Live page:

- **Three cards under "Active now"**, `support-agent`, `billing-agent` and
  `accounts-agent`, each with its run id, the most recently active first.
- **The newest activity is followed**: the canvas and the Inspector switch
  to whichever agent sent the latest observations ("Following newest
  activity").
- **Click a card to follow that agent.** The view stays on it ("Pinned to
  your selection") while the other cards keep counting.
- **Follow active** goes back to following the newest activity.

Live mode's model is **`qwen2.5:3b`**, not `gemma3:4b`: with three workers on
one Ollama, `gemma3:4b` wrote prose into every field of the action schema and
took 100–290 s a ticket, `qwen2.5:3b` 10–14 s at the median. `OLLAMA_MODEL`
overrides it, and `make live` pulls the model when it is missing, as `make
demo` does.

What a 12-minute run showed (Trustvian v0.12.0, `qwen2.5:3b` on an Apple M3
Pro laptop, the Ollama app with its default settings):

- **Three cards, one per agent**, and three runs, each `completed` after
  Ctrl-C, which took about a second; no dev, Collector, agent or mock service
  was left running.
- **A steady stream.** 93 tickets (33, 26 and 34), about 1,140
  observations; a ticket took 12.7 s at the median and 25 s at most. The
  agents never fall behind — each finishes its ticket before it waits for
  the next — but a slower model means fewer tickets.
- **Each agent's behaviors, in its own run:** `external/qwen2.5:3b → ollama`
  and `http/POST → ollama.localhost` (the planner's model call and its
  transport), `http/GET` and `http/POST → crm.localhost`, and the agent's
  tools: 6 for `support-agent` and `accounts-agent`, 9 for `billing-agent`
  (its three tools, and the drift's `export_customer` with its
  `http/POST → export.localhost`).
- **The drift** came once, at billing ticket 20.
- **Failed calls:** the ticket lines counted 22 refused tool calls (5 for
  `support-agent`, 14 for `billing-agent`, 3 for `accounts-agent`): the
  injected `503`/`429`, and actions the services refused (13 of the 35
  `issue_refund` calls failed). 40 observations carry an error status:
  those tool spans, the HTTP requests beneath them, and the two model calls
  Ctrl-C cut off. The runs went on.
- **Pinning held** (in a 3-minute Compose run). With the view following
  `billing-agent`, a click on `support-agent` read "Pinned to your
  selection"; 20 s later it still showed `support-agent` while the other
  cards kept counting, and **Follow active** went back to "Following newest
  activity".
- **Status shows one of the three.** Every dev's Collector reports to the
  control plane as `dev`, and the control plane holds one report per name,
  so `trustvian status` and the Status view show one agent's Collector at a
  time: six reads 5 s apart, natively and under Compose, named
  `accounts-agent` every time. Each count is that one Collector's
  (`"received": "48", "evaluated": "48"`), not the fleet's.

What the Live view does not do at Trustvian v0.12.0:

- **No pause.** The view keeps moving; to hold one agent still, pin its card.
  (The **Stop** under "Watch one run" stops watching a run, not the view.)
- **The canvas draws from the moment you select a run.** The stream keeps
  no history, so a pinned card's canvas fills as new observations arrive (it
  read only `Drawing run dev-live-support-…` right after the click, and the
  support tools 20 s later); a one-off such as the drift is drawn only if it
  arrives while that run is shown. The Live stream table below lists every
  agent's rows, whichever card is pinned. Earlier evidence is under Runs.

If the Live page stays empty, ask the pipeline: `.demo/bin/trustvian status
--api-url http://127.0.0.1:8080` (or the WebUI's **Status** view) names the
Collector reporting, the spans it received and evaluated, the `service.name`
it saw, and suggestions naming what to check; with no Collector at all it
says `No Collector has reported in 30 s. Is 'trustvian dev' running, and is
trustvian-collector on its path?`. `.demo/bin/trustvian dev --check` starts a
control plane and a Collector with no workload: a reporting `dev-check`
Collector and an empty `suggestions` list mean the helpers work.

| Variable | Default | |
|---|---|---|
| `TRUSTVIAN_API_URL` | none; required | the control plane every dev attaches to |
| `LIVE_AGENTS` | `support,billing,accounts` | the roles to run, one dev each |
| `LIVE_INTERVAL_SECONDS` | `10` | one ticket per agent this often, ±50% jitter |
| `LIVE_DRIFT_EVERY` | `20` | every Nth billing ticket also exports; `0` = never |
| `LIVE_MAX_TICKETS` | `0` | tickets per agent, then that run completes; `0` = until stopped |
| `LIVE_STOP_GRACE_SECONDS` | `20` | how long Ctrl-C waits for the runs to complete |
| `CRM_ERROR_RATE` | `0.05` | share of mock-service requests that fail; `0` = none |
| `OLLAMA_MODEL` | `qwen2.5:3b` | live mode's model |

The three agents share one Ollama, which serves `OLLAMA_NUM_PARALLEL`
requests at a time and queues the rest, so a ticket can wait for the other
agents' model calls, and the median above includes that wait.
Each role learns into its own baseline under `~/.trustvian/dev/`, kept
between sessions, so the next session is scored against what this one
taught.

With Docker Compose, from `compose/`, live mode is a service of its own: a
control plane in its container on `http://127.0.0.1:8080/`, and `live.py`
with its three dev processes and three Collectors attached to it, using the
host's Ollama (see [compose/README.md](compose/README.md#live-mode)):

```bash
docker compose --profile live up -d --build
docker compose logs -f live               # the run ids and the per-ticket lines
docker compose --profile live down        # stops it; every dev completes its run
```

`down` keeps the `live-state` volumes, so the next `up` is scored against what
this one taught; `down -v` forgets it. `LIVE_OLLAMA_MODEL` picks its model.

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

  Before any model-driven run starts, the orchestration **asks the model one
  question and requires an answer**, on `ollama.localhost:11434` — the address
  `agent/planner.py` itself calls. That is stricter than it sounds, and each part
  of it was a real defect:

  | Cheaper check | Passes when the agent cannot work |
  |---|---|
  | a TCP connect | a **suspended** server keeps its listening socket, so the kernel completes the handshake and nothing ever answers |
  | `GET /api/version` | answers without the model being loaded, or present |
  | `ollama list` | reads metadata from disk; a model too large for available memory is listed, then fails on first use |
  | probing `127.0.0.1` | `ollama.localhost` resolves to `::1` **first** on macOS while Ollama binds IPv4 only — so the agent's calls work only because the client retries the next address. Where `::1` is filtered rather than refused, every model call hangs while a `127.0.0.1` probe stays green. |

  A port held by something that does not answer is reported as exactly that,
  with the `ps` command that shows a stopped process — not as the `address
  already in use` a second `ollama serve` would produce.

  The reply itself is never printed: it is a model completion, and no completion
  appears in anything this repository publishes.

No Docker: the demo itself never needs it, and [`compose/`](compose/README.md)
is an optional wrapper for a machine that has Docker and nothing else. No API
keys. No `sudo`. No external service but Ollama.

Three gitignored directories in this repository hold what it generates —
`.demo/` (binaries and virtualenvs), `.runtime/` (logs and per-run scratch) and
`.trustvian/` (the control-plane database `scripts/runtime.sh` starts).

**`trustvian dev` keeps its own state outside this repository**, under
`~/.trustvian/dev/<hash of this directory>/`: the generated Collector
configuration, both helper logs, and the learned baseline for each candidate. dev
prints that path on the `State` line of every start, and it is deliberate —
dev's working directory is the application's repository, and task 077's
acceptance criterion 2 is that the repository is byte-identical afterwards.

`make clean` removes the three above and **names** dev's path without removing
it. Deriving a hash-keyed path under `$HOME` a second time is how `rm -rf`
reaches the wrong directory, so this repository prints what dev reported and
stops there.

The Go and Python toolchains write to their usual user-level caches, which is
what `go build` and `pip install` do.

### Required layout

```text
trustvian-workspace/
├── trustvian/                      # the Trustvian repository
└── trustvian-python-agent-demo/    # this repository
```

Set `TRUSTVIAN_DIR` to override. `scripts/bootstrap.sh` fails immediately, by
name, if the checkout is missing or does not carry the Collector's evaluation
sink.

### Or a downloaded release, with no Go toolchain

Trustvian's release archives for macOS and Linux carry `trustvian-local` and
`trustvian-collector` beside `trustvian`, so this demo can run without building
anything:

```bash
tar xzf trustvian_v0.12.0_darwin_arm64.tar.gz
TRUSTVIAN_RELEASE_DIR=$PWD/trustvian_v0.12.0_darwin_arm64 make smoke
```

`make release-smoke TRUSTVIAN_RELEASE_DIR=<dir>` does that end to end from a
clean `.demo/bin`, so it cannot pass on binaries a checkout build left behind.

Setting both `TRUSTVIAN_DIR` and `TRUSTVIAN_RELEASE_DIR` is an error naming
both, not a precedence rule: two sources of the same three binaries is the
ambiguity that produces "which Trustvian did I just measure?", and every number
in `docs/results/` is attributed to one commit.

**The checkout stays the default, and CI only ever uses it.** This repository
exists to catch a Trustvian change that breaks it, and a release-pinned CI job
would be blind to precisely that. The release path is for reproducing a
published measurement, and it is hand-tested rather than guarded — which is why
`make release-smoke` exists as a target you can run rather than a job that runs
itself.

### Upgrading from Trustvian v0.10.0

`make bootstrap` rebuilds `.demo/bin` when the release (or the checkout's
commit) changes, so the binaries follow `TRUSTVIAN_RELEASE_DIR`. Two things do
not follow by themselves:

**The platform database moves one way.** The first v0.12.0 `trustvian-local`
to open a v0.10.0 state migrates it from schema 9 to 15, and v0.10.0 then
refuses it:

```text
trustvian-local: opening platform database: platform: unsupported persistence schema: database reports version 15, this build supports 9
```

`make demo`, `make smoke`, `make scenario` and `make model-change` start a
fresh `.trustvian/` every time, so they carry nothing over. Live mode's state
does carry over: `.demo/live/` natively, and the `live-state` volume under
Compose. Copy it while its control plane is stopped if it matters; to start
fresh, `rm -rf .demo/live` (`make clean` removes it with the rest of
`.demo/`), or `docker compose --profile live down -v` in `compose/`. Rebuild
the Compose images, the live service's included:

```bash
cd compose && docker compose build demo && docker compose --profile live build live
```

**Upgrade the control plane before the CLI.** A v0.12.0 `trustvian eval run`
sends fields an older control plane refuses, before any repetition starts
(`trustvian: invalid_request: request body is not valid JSON for this
endpoint`). Every target here starts its control plane from `.demo/bin`
except live mode's, which you start by hand: restart it after `make
bootstrap`, and check it with a route v0.12.0 added, which an older control
plane answers with `404`:

```bash
.demo/bin/trustvian status --api-url http://127.0.0.1:8080 >/dev/null && echo "v0.12.0 or later"
# against an older one: trustvian: server returned HTTP 404   (exit 3)
```

### Bootstrap is idempotent

`scripts/bootstrap.sh` runs at the head of everything, so it skips what is
already current: the Go build when the checkout's commit matches the stamp
beside the binaries, and pip when the installed set matches a hash of
`agent/requirements.txt` and the pinned OpenTelemetry versions. A dirty
Trustvian worktree never matches, so uncommitted changes there always rebuild.

Measured 2026-09-27 on an Apple M-series laptop, with a warm Go build cache and
a warm pip cache, against Trustvian `5362f51`:

```text
bootstrap, everything already current    0.18 s     no network
bootstrap, .demo/ removed entirely      14.6  s     three Go binaries, two venvs

make smoke   (fixture, no model)        10.1 s      full lifecycle + 104 tests
make demo    cold, to its result       149   s
make demo    warm, to its result       155   s
```

**The demo's cost is the model, not the setup.** The warm run came out *slower*
than the cold one, which is the honest way to say that bootstrap's 14 s sits well
inside the variance of six model-driven bounded loops. Treat 2–3 minutes as the
figure and the 14 s as noise.

`make demo` is timed *to its result* rather than to its exit, because it holds
the control plane open for the WebUI until Ctrl-C by design.

## Commands

```bash
make demo        # the full interactive demo, leaves the runtime up
make scenario    # run a scenario file unattended
make model-change  # llama3.2 against gemma3:4b through trustvian eval run, as a report
make stability   # measure an unchanged agent against itself: RUNS=10 TEMPERATURE=0.7
make fidelity-sweep  # the tool-fidelity re-run: RUNS TEMPERATURE TIMEOUT RESULTS, all required
make live        # live mode: three agents, one run each; needs TRUSTVIAN_API_URL
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
4. Prints the WebUI URL and **waits for ENTER**, so the browser is open before
   anything is produced.
5. Runs the reference evaluation through `trustvian dev`, which creates the
   project, agent, environment and candidate on the way — each only where it is
   missing. The hierarchy still exists before any telemetry does; it is now
   created by dev at the first run rather than by this repository beforehand,
   which is one thing retiring the stand-in gave up.
6. **Waits for ENTER again**, then runs the candidate the same way.
7. Both runs attach to the control plane from step 2 with `--api-url`.
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
scripts/runtime.sh up      # or: .demo/bin/trustvian-local --state-dir .trustvian
```

Clients in this directory then need no `--api-url`: `trustvian eval compare` and
friends read `.trustvian/runtime.json`. dev publishes a second discovery location
under its own state directory, which the CLI falls back to when this directory
has none — useful when dev started the control plane itself, and only while it is
still running, since a clean shutdown removes its own discovery file.

The next `make demo` resets `.trustvian/`, so treat this as a way to revisit
existing evidence rather than to accumulate it across runs.

## What Trustvian actually sees

```text
external · gemma3:4b -> ollama   the model call, named by the harness — model-driven runs only
POST -> ollama.localhost     the same model call's HTTP request — model-driven runs only
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

* Observed in verified runs on 2026-09-27, not a guarantee: the model decides
  how many turns to take. The behavior set is stable at temperature 0. The
  fixture's counts are asserted exactly by `make smoke`; the model's are
  reported.
```

The 2026-09-27 model counts predate the harness's `execute_tool` and `chat`
spans, each of which adds a behavior beside the HTTP one it wraps. The model
call is therefore counted under two targets: the `chat` span's `ollama` and the
HTTP request's `ollama.localhost`, with the same number of calls per run.

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
`trustvian dev` for every launch. Without it the `requests`
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
| `trustvian dev` (077) | `trustvian --help` offers `trustvian dev` — **required**, not optional: there is no fallback, so a build without it fails here rather than later |
| Scenario runner (078) | `trustvian eval --help` mentions a scenario |
| Tool fidelity (075) | a comparison response describes a `tool` behavior |

When a capability is missing, the output says so plainly. When one arrives, the
stand-in for it says it should be retired — and when one becomes a hard
requirement, as `trustvian dev` now is, the probe stops the run instead.

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
fixtures/deterministic_agent.py  the model-free driver CI runs
fixtures/stochastic_agent.py     a SEEDED SIMULATION, never evidence about a model
mock_services/server.py    deterministic local backend services
scenarios/                 scenario files, task 078's shape
scripts/lib.sh             shared primitives, dev_run, and the capability probes
scripts/bootstrap.sh       builds Trustvian, creates the environments
scripts/runtime.sh         the control plane many runs attach to
scripts/clean.sh           make clean
scripts/demo.sh            make demo
scripts/smoke.sh           make smoke
scripts/live.sh            make live
live.py                    live mode's supervisor: one trustvian dev per role
agent/roles.py             live mode's roles, tickets and per-worker loop
tools/tvdemo/              the scenario runner, the stability sweep, this repo's tooling
docs/results/              measured numbers, committed
tests/                     the agent's unit tests
tools/tests/               the tooling's unit tests
docs/design/               what is being built, and why
docs/upstream/             proposals for Trustvian, citing the task number
```

## Cleanup

```bash
make clean
```

Removes `.demo/`, `.runtime/` and `.trustvian/`, then prints the path
`trustvian dev` last reported for its own state — which it does **not** remove,
because this repository did not derive that path and will not delete one it was
only told about.

A previous `make demo` left running does not block a new one: the new run stops
that runtime and starts its own, reporting both steps. It only ever stops a
runtime serving this directory's `.trustvian/`, so a `trustvian-local` you
started yourself is never touched — and neither is one `trustvian dev` started,
which keeps its state elsewhere.

## License

Apache-2.0. See [LICENSE](LICENSE).

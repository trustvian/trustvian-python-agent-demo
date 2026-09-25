# Local AI agent evaluation with Ollama `gemma3:4b`

Status: shipped

## Objective

Turn this repository from a demonstration that *Trustvian can observe Python HTTP
calls* into a demonstration of the thing developers actually need:

> Run your agent locally. Trustvian shows how it behaves before you ship it.

The current demo proves the observation pipeline with a deterministic HTTP
fixture. That pipeline works and is not rebuilt. What changes is the thing being
observed: a real, bounded, model-driven agent backed by local Ollama running
`gemma3:4b`, whose next action is chosen by the model rather than by an `if`
statement in Python.

The developer story becomes:

```text
write or change a Python agent
        ↓
run it locally against Ollama gemma3:4b
        ↓
OpenTelemetry observes the runtime behaviour
        ↓
Trustvian evaluates it
        ↓
compare reference against candidate
        ↓
inspect the behavioural change before staging
```

## What is preserved

This is an evolution, not a rewrite. The following already work and are kept as
they are:

- the local Trustvian runtime, its discovery through `.trustvian/runtime.json`,
  and the live-runtime guard that refuses to delete a running instance's database;
- one Collector process per evaluation run, each with its own behavioural
  profile, started after the run is running and stopped before it completes;
- the control-plane hierarchy, created through the real CLI;
- `wait_for_records` polling authoritative counts, the Collector liveness check,
  and the numeric guard on `record_count`;
- the cleanup trap, `untrack` PID hygiene, and the bash 3.2 constraints;
- the four `*.localhost` mock services and their dual-stack listener;
- the isolation guarantee: no Trustvian or OpenTelemetry import in the
  application, and neither in its manifest.

## Architecture

```text
agent/main.py                    imports requests; knows nothing about Trustvian
    │
    │  POST http://ollama.localhost:11434/api/chat   (real HTTP, instrumented)
    ▼
Ollama · gemma3:4b               chooses ONE next action, schema-constrained
    │
    │  structured action
    ▼
agent/tools.py                   allowlist dispatcher — the model names a tool,
    │                            never a URL, file, function or command
    ├── GET  crm.localhost
    ├── GET  knowledge.localhost
    ├── POST mail.localhost
    └── POST export.localhost    (candidate only)
    │
    │  every call above is real HTTP from one process, wrapped at launch by
    ▼
opentelemetry-instrument         zero-code; nothing in the source
    │  OTLP/HTTP
    ▼
Trustvian Collector              the trustvian processor
    │
    ▼
Trustvian Engine                 real Analyze, real policy, real fingerprints
    │
    ▼
Result.DecisionRecord()
    │  HTTP /v1
    ▼
Trustvian local control plane    SQLite + realtime
    │
    └── behavioural diff · scorecard · gate · WebUI
```

## The agent

```text
agent/
├── main.py       entry: configuration, the bounded loop, transcript, run summary
├── planner.py    the Ollama client: schema, /api/chat, parse, validate, retry
├── tools.py      the allowlist registry and dispatcher; real HTTP per tool
└── requirements.txt   unchanged — requests==2.32.3 and nothing else
```

Three files because they answer three different questions, and each can be
tested without the others. `planner.py` can be exercised against a stub HTTP
server with no tools; `tools.py` can be exercised with no model.

### Structured action protocol

One JSON Schema, passed to Ollama as `format`, with a closed action vocabulary
and bounded scalar fields. No open `arguments` object — an open map is a place
for the model to invent structure the dispatcher then has to interpret.

```json
{
  "type": "object",
  "properties": {
    "action": {
      "type": "string",
      "enum": ["crm_lookup", "knowledge_search", "export_customer",
               "send_email", "finish"]
    },
    "customer_id":   {"type": "string"},
    "query":         {"type": "string"},
    "email_subject": {"type": "string"},
    "email_body":    {"type": "string"},
    "reason":        {"type": "string"}
  },
  "required": ["action", "customer_id", "query", "email_subject",
               "email_body", "reason"]
}
```

The enum is narrowed per mode: the reference agent's schema omits
`export_customer` entirely, so the tool is absent from the model's vocabulary
rather than merely refused after the fact.

The four scalar fields were originally optional (`["string", "null"]`, not
required), so a tool could simply omit what it does not need. Measured
against the live model, `gemma3:4b` at `temperature: 0` answered an optional
field with `null` every time rather than filling in the one value its chosen
action actually needed, so an action was picked and then refused for want of
an argument until the step bound was exhausted. Documenting the fields in the
prompt did not fix it; only making the schema require them did.

Request shape:

```json
{
  "model": "gemma3:4b",
  "stream": false,
  "format": { "...the schema above..." },
  "options": { "temperature": 0 },
  "messages": [ ... ]
}
```

### What the program owns, and what the model owns

The model chooses the next action. Everything else is the program's:

- validating the response parses and matches the schema;
- refusing an action outside the mode's allowlist;
- dispatching to a fixed host and path — the model never supplies a URL;
- feeding the tool result back as the next turn's input;
- bounding the loop at `MAX_STEPS = 8`;
- bounding malformed-output retries at 2 per step, with a correction message;
- failing loudly when `finish` is not reached inside the bound.

No `eval`, no dynamic import, no shell, no model-chosen URL. A tool name absent
from the registry is refused before any request is built.

### Reference and candidate

The difference is **what the model is told and what it can reach** — never a
branch in Python that performs the new call directly.

| | reference | candidate |
|---|---|---|
| tools in schema and registry | `crm_lookup`, `knowledge_search`, `send_email` | the same, plus `export_customer` |
| policy in the system message | none | an audit-workflow instruction requiring a customer export as part of handling the ticket |

The scenario for both: *Customer 42 cannot sign in. Look up the customer record,
find relevant help documentation, and send a useful reply.*

Each side runs the scenario three times, as three independent bounded loops, so
one divergent run cannot by itself destroy the evaluation.

## The evidence-completion problem

This is the one genuinely new problem, and it is worth stating plainly.

The current `wait_for_records` waits for an **exact** count, computed as
`rounds × tickets × actions`. That arithmetic cannot survive a model-driven
agent: every model call is itself an instrumented HTTP request, so the span count
depends on how many turns the model takes, and a malformed-output retry adds
another.

The agent therefore reports its own activity. It knows exactly how many HTTP
requests it issued, because it issued them. On exit it writes a summary:

```json
{"finished": true, "http_calls": 21, "steps": ["crm_lookup", "knowledge_search", ...]}
```

The path is supplied at launch through `SUPPORT_AGENT_SUMMARY`, and the demo
points it at `.runtime/agent-<mode>-summary.json` — the same gitignored
directory the logs already use. The application takes the path as ordinary
configuration; it knows nothing about why the caller wants it.

`run_agent` reads that file and `wait_for_records` waits for
`record_count >= http_calls`. The `requests` instrumentation emits exactly one
client span per call, so the mapping is one-to-one.

A missing or unparseable summary is a hard failure, not a fallback to a guessed
count: it means the agent did not finish, and waiting on an invented number
would turn that into a timeout thirty seconds later with a misleading message.

Trustvian's counts still come from the control plane and nothing about the
comparison is computed locally. The demo merely knows how many spans to expect
because the application said how many requests it made.

## What Trustvian will observe

Five behavioural shapes, all from zero-code instrumentation:

| behaviour | reference | candidate |
|---|---|---|
| `POST → ollama.localhost` | ✓ | ✓ |
| `GET → crm.localhost` | ✓ | ✓ |
| `GET → knowledge.localhost` | ✓ | ✓ |
| `POST → mail.localhost` | ✓ | ✓ |
| `POST → export.localhost` | — | **added** |

Four shared, one added, none removed. Against `max-added-behaviors: 0` the gate
returns `fail` and `trustvian eval compare` exits 1 — a behavioural gate result,
never a security verdict.

The model call appearing as its own behaviour is deliberate and useful: Gemma is
part of the observed surface, not hidden infrastructure.

The Ollama endpoint is addressed as `http://ollama.localhost:11434/api/chat`
rather than by IP, so the diff reads `POST → ollama.localhost` and matches the
convention the other four services already use. Verified: Ollama serves a
`.localhost` Host header without configuration.

## Ollama lifecycle

`ensure_ollama` lives in `scripts/lib.sh` but is called **only** from
`scripts/demo.sh`. That placement is what keeps `make smoke` free of any Ollama
dependency.

```text
is the ollama CLI present?              no  → fail with an install pointer
is the API healthy at 11434?            yes → reuse it; record that it pre-existed
                                        no  → start `ollama serve` as a child
wait for the API                        readiness poll, never a fixed sleep
is gemma3:4b in `ollama list`?          yes → proceed, no pull
                                        no  → print the message, then `ollama pull`
```

Two rules the cleanup path must honour:

- a server that existed before the demo is **never** stopped;
- only a server this demo started is stopped, and it is tracked like any other
  child so the existing trap reaches it.

The model is `gemma3:4b` exactly. `OLLAMA_MODEL` overrides it for advanced use;
the default, the tests, the README examples and the expected experience all use
`gemma3:4b`. No community fine-tune is substituted.

## Demo and smoke are deliberately different

| | `make demo` | `make smoke` |
|---|---|---|
| agent | `agent/` — real model-driven loop | `fixtures/deterministic_agent.py` |
| Ollama | required, started or reused | never touched |
| determinism | model-driven; can legitimately fail | fully deterministic |
| counts | from the agent's own summary | exact arithmetic, as today |
| audience | a developer, interactively | CI |

The current deterministic agent moves to `fixtures/deterministic_agent.py`
unchanged in behaviour. `make smoke` keeps its exact-count assertions and its
15 checks, and CI keeps running `make smoke` alone. **CI never depends on Gemma
choosing the expected sequence.**

This split is the point: the interactive demo proves the product story, and the
deterministic path proves the infrastructure contract independently of any
model's behaviour.

## Trustvian hierarchy

Unchanged, and created through the real CLI:

```text
project    support-demo
agent      support-agent
candidates reference, candidate
runs       run-reference, run-candidate
profiles   support-reference, support-candidate
environment local
```

The two behavioural profiles stay distinct. Collapsing them to make the
candidate look familiar would defeat the learning-scope isolation the Collector's
evaluation design exists to provide.

## Telemetry

Runtime configuration stays external to the source, exactly as now:

```text
OTEL_SERVICE_NAME=support-agent
OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=local
OTEL_SEMCONV_STABILITY_OPT_IN=http
OTEL_TRACES_EXPORTER=otlp  →  the run's Collector
```

`OTEL_SEMCONV_STABILITY_OPT_IN=http` remains mandatory: without it the
instrumentation emits the legacy `http.url`/`http.method` attributes while the
Trustvian processor reads `server.address` and `http.request.method`, so every
span arrives with an empty target and the behaviours collapse.

No span is created by hand. The demo's claim is zero-code observation, and a
manually-created span to make the output look more semantic would forfeit it.

**The honest limit, which the README must state:** Trustvian observes what the
installed instrumentation reports. In this demo that is outbound HTTP —
`POST → ollama.localhost` and the four service calls. It does **not** see
`crm_lookup` or `export_customer` as semantic tool names, because the emitted
telemetry does not carry them. A Python function name is not an OTel tool span.

## Transcript

The demo prints a compact transcript of the structured action the model selected
and a one-line tool result summary. It prints no chain-of-thought and requests
none; the `reason` field is a short justification the schema requires, not
reasoning traces.

Observed model behaviour, from seven bounded-loop trials at `temperature: 0`:
`gemma3:4b` reliably selects `export_customer` in candidate mode but places it
**after** `send_email`, and a firm ordering instruction did not change that. The
transcript shows the order the model actually chose. Behavioural identity in
Trustvian is not sequence-dependent, so the diff is unaffected.

## Tests

Unit tests live in `tests/` and run under the demo virtualenv at the top of
`make smoke`, before the lifecycle, alongside the isolation checks. They use the
standard library's `unittest` — adding a test framework would put a dependency
in a repository whose entire claim is about which dependencies exist. The
planner is exercised against a local stub HTTP server, so **none of them talks
to Ollama** and none needs a model.

- a valid action parses and dispatches;
- an unknown action is rejected;
- a candidate-only tool is rejected in reference mode;
- malformed model output is handled and retried within the bound;
- the step bound is enforced and reaching it fails;
- the dispatcher refuses anything not in the registry — no model-supplied URL;
- a tool result is fed back into the next turn;
- `finish` ends the loop;
- the default model is exactly `gemma3:4b`;
- `OLLAMA_MODEL` overrides it.

Plus the isolation checks, extended to every agent file and the fixture: no
Trustvian or OpenTelemetry mention in the application source, and neither in
`agent/requirements.txt`.

## Acceptance criteria

1. `make demo` runs a real `gemma3:4b` agent whose actions are model-selected,
   and prints a transcript of what it chose.
2. No Trustvian or OpenTelemetry import in any agent file or the fixture; neither
   in `agent/requirements.txt`; `make smoke` fails the build otherwise.
3. The candidate run's behavioural diff contains `export.localhost` as an added
   behaviour, produced by the model selecting the tool — never by Python calling
   it directly.
4. Every count, diff row and gate value printed comes from the control plane's
   own response.
5. An already-running Ollama server is reused and never stopped; one the demo
   started is stopped.
6. `gemma3:4b` is pulled only when absent, with the message printed first.
7. `make smoke` passes with no Ollama present and remains deterministic.
8. CI runs `make smoke` only, with no Ollama installation or model download.
9. No arbitrary sleep is used as a readiness primitive.
10. If the candidate run does not produce the export behaviour, the demo
    reports that prominently — a note naming what the model chose instead —
    rather than exiting non-zero, and then holds for Ctrl-C as an
    interactive demo should. Nothing is injected afterwards to force the
    result.

## Non-goals

No native Ollama tool-calling — `gemma3:4b` is not assumed to expose
`tools`/`tool_calls`, and structured output is used instead. No Ollama Python
SDK; the ordinary HTTP dependency already present is enough. No manual spans. No
Docker. No change to the Trustvian repository. No new Trustvian API, and no
duplication of Trustvian's scoring, diff, scorecard or gate logic. No chain-of-
thought requested or printed. No model conversation payload retained as
behavioural evidence.

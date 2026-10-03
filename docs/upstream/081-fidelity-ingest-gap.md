# Fidelity never reaches the control plane through the Collector

**A proposal for [`trustvian`](../../../trustvian), not a change to it.** Nothing in
this repository modifies that checkout;
[the design doc](../design/2026-09-26-behavioral-regression-demo.md)'s invariant 4
makes a gap that needs Trustvian work become a document here instead.

Recorded 2026-09-28, against Trustvian `d84ce43`. This is the record of **how
the gap was found**, kept for that reason: the fix itself is being made upstream
as its own pull request.

## What happens

Task 075 shipped a fidelity indicator — `semantic` when an agent-oriented
convention supplied an operation's identity, `transport` when only protocol and
target were available. On the live view, every record reads `transport`,
including the ones whose identity provably came from a semantic convention.

Observed from a real run: a `gemma3:4b` agent under the harness wrapper, one run
per side, with the realtime SSE stream captured live. The mapping is correct —
the behavior is `tool · export_customer`, which only `execute_tool` +
`gen_ai.tool.name` can produce — and the indicator that should qualify it says
`transport`:

```
   6  cat='tool'  name='crm_lookup'       target=None  fidelity='transport'
   3  cat='tool'  name='export_customer'  target=None  fidelity='transport'
   6  cat='http'  name='GET'   target='crm.localhost'  fidelity='transport'
```

## Where it is lost

Not in the mapping, and not in the platform. In the one hop between them.

| Layer | State |
|---|---|
| `internal/semconv/genai.go` | sets `FidelitySemantic` on the `Normalized` — correct |
| `processor/mapping.go:124` | writes `trustvian.fidelity=semantic` onto the Event's attributes — correct |
| `processor/internal/evaluation/client.go:255` | `ingestEnvelope` has `version`, `sequence`, `behavioral_profile`, `record` — **and no `fidelity` field** |
| `trustvian.DecisionRecord` | has no `Attributes` field, so the value is not recoverable from the record either |
| `platform/httpapi/dto.go:299` | accepts `fidelity` on the envelope, optional, absent meaning transport — ready and waiting |
| `platform/webui/assets/live.js:230`, `:252` | reads `observation.fidelity \|\| "transport"` |
| `platform/webui/assets/inspector.js:76` | states a sentence when it is `semantic` |

Both ends are complete. The Collector simply never sends the field, and the
value is not on the wire in any other form, so nothing downstream could recover
it.

**Consequence:** the indicator task 075 shipped reaches no operator through the
Collector ingest path. It works only for a producer that POSTs to the ingest
endpoint directly and fills the field itself — which is what task 075's own
tests do, which is why this was not caught there.

## The shape of the fix

`fidelity` rides beside the record in the processor's envelope, exactly as
`behavioral_profile` already does and for the identical stated reason — it is
metadata about how the adapter derived the record, not anything the engine
decided.

The sink's signature is `Record(ctx, record trustvian.DecisionRecord, learning
[]byte)`, and `DecisionRecord` carries no attributes, so the value has to travel
alongside rather than inside. The processor has the whole `Result` at the call
site (`processor/processor.go:528`) and can read
`result.Event.Attributes[event.AttrFidelity]` there.

Two things worth deciding rather than assuming:

- **Whether `DecisionRecord` should carry fidelity instead.** It would put the
  value where every consumer already looks, and it would make the record
  self-describing. But it is a core change to a published type, and ADR 0024
  kept learning scope out of the record for a comparable reason. Passing it
  beside the record keeps the core untouched, which is what task 075 committed
  to.
- **Whether a route should expose per-behavior fidelity.** That is task 081, and
  it needs a forward-only schema step in both backends. This gap is narrower: it
  is about the value reaching the control plane at all, which is a precondition
  for 081 rather than part of it.

## How to verify a fix

The check that found it, which is cheap to repeat:

1. Run a workload emitting `gen_ai.operation.name=execute_tool` and
   `gen_ai.tool.name` through the Collector — `make scenario
   SCENARIO=scenarios/support-demo.yaml` in this repository does exactly that.
2. Capture `GET /v1/realtime` **during** the run. The stream is live-only;
   `stream_ready` publishes `replay_available: false`, so a subscription opened
   afterwards gets nothing.
3. A record whose `behavior.operation_category` is `tool` must report
   `fidelity: "semantic"`.

Step 2 is the part that is easy to get wrong. A capture attached before the run
also fails, because `start_runtime` stops any runtime serving the directory's
`.trustvian/` and the scenario starts its own on a different port — so the
capture has to attach to the runtime the scenario itself publishes.

## Status in this repository

The live-view check will be re-run once the upstream fix lands, and
[`docs/results/2026-09-29-stability-tool-fidelity.md`](../results/2026-09-29-stability-tool-fidelity.md)
states which fidelity the sweep's records actually carried at the time it ran,
rather than what the mapping determined. The distinction matters: the sweep's
behavior *names* are semantic — that is what makes it a tool-fidelity
measurement — and only the indicator qualifying them is missing.

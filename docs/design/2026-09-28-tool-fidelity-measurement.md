# Measuring stability at tool fidelity

2026-09-28. Design only — nothing in this document is implemented yet, except
the throwaway spike described in § 3, which was run to answer a question that
could not be answered on paper and was then deleted.

## Why there is a second sweep

[`docs/results/2026-09-27-stability.md`](../results/2026-09-27-stability.md)
measured how often an unchanged agent fails a gate against itself, and got
zero: 0/9 adjacent-pair and 0/90 all-pairs FAILs in all four
configuration-temperature combinations, with every behavior seen in 10/10 runs.
[`docs/upstream/078-measurement.md`](../upstream/078-measurement.md) reported
that, and named the two conditions under which it should be re-run. Task 078 in
the engine repository kept that section
(`../trustvian/docs/tasks/v1.0/078-behavioral-scenario-suites.md`
§ "The section stays, with two re-run conditions"):

1. **A toolset wider than one run visits.** Three tools that every ticket needs
   cannot produce presence variance. The measurement said so about itself.
2. **Tool-name fidelity.** At transport fidelity a behavior is `POST ·
   export.localhost`, and the host is fixed by the dispatcher rather than
   chosen by the model. 078 now marks this one "**It is now available.**" —
   task 075 shipped, and `internal/semconv` reads
   `gen_ai.operation.name=execute_tool` + `gen_ai.tool.name`.

Both conditions are now addressable, and they compound: a wide toolset matters
only if the thing that varies is visible, and tool names are only interesting if
the model has a real choice among them. This document plans both together.

## 1. How tool calls become `execute_tool` spans

### The constraint that decides this

`agent/` imports neither Trustvian nor OpenTelemetry, and
`agent/requirements.txt` declares neither. That is asserted by `make smoke` over
`APPLICATION_SOURCES` (`scripts/smoke.sh:46`). It is the whole claim the demo
makes, so the span cannot come from inside the agent.

### Option (a): a harness-side pass-through wrapper — recommended

Task 080 in the engine repository already fixes this shape
(`../trustvian/docs/tasks/v1.0/080-*.md` § "The expected path: a pass-through
wrapper", and its "What 'unmodified' means"): wrapping a dispatch function from
the harness at import time is explicitly *allowed*, where editing source,
forking, vendoring, or altering behaviour or order is not. The wrapper must
preserve arguments by value and position, the return value, every raised
exception with type and payload, and the order and number of dispatches.

`agent/main.py` does `from agent import tools` and calls `tools.dispatch(...)`
(`agent/main.py:120`) — a module-attribute lookup at call time. Rebinding
`agent.tools.dispatch` before `agent.main.main()` runs is therefore sufficient,
and requires no cooperation from either module.

Attributes emitted, and only these:

| attribute | value |
|---|---|
| `gen_ai.operation.name` | `execute_tool` |
| `gen_ai.tool.name` | the tool name the planner chose |

No arguments, no return value, no `gen_ai.tool.description`, no
`gen_ai.tool.definitions` — the list 080 gives, unchanged.

**Nesting.** The brief asked for the `execute_tool` span "nested under the
current HTTP span so the mock-service call stays visible". That is the right
intent and the wrong direction, and the direction is not a matter of taste: the
HTTP span is created *inside* `dispatch`, by the `requests` instrumentation,
when `session.get`/`session.post` runs. A wrapper around `dispatch` opens its
span before that and closes it after, so `execute_tool` is necessarily the
**parent** and the HTTP CLIENT span its child. This achieves exactly what was
asked — both spans exist, the mock-service call stays visible — and it is the
nesting the GenAI convention describes. Inverting it would require emitting the
tool span from inside the HTTP call, which means instrumenting `requests`
ourselves.

**One content trap, found by building it.** `start_as_current_span` defaults to
`record_exception=True`, which attaches `str(exc)` to the span as an event. A
`ToolError` message quotes the model's own argument — `agent/tools.py`'s
`_require_id` raises ``f"{field} {value!r} is not a valid identifier"``. That is
model-chosen content on a span, and it would travel. The wrapper must pass
`record_exception=False` explicitly. `set_status_on_exception` stays on: a
status code is content-free, and the processor bridges it to the `error=true`
volatile signal, which is a signal we want.

### Option (b): port the agent to an instrumented framework

LangChain with OpenLLMetry or OpenInference instrumentation, or Pydantic AI,
would emit tool spans with no wrapper at all, because the framework owns the
dispatch point.

What (b) would add, for external users rather than for this measurement:

- It is the path an actual adopter is on. Nobody writes a bespoke dispatcher
  and then wraps it; they use a framework, and the question they ask is "does
  Trustvian read what my framework already emits". A LangChain run would
  exercise the OpenInference half of `internal/semconv` — `openinference.span.kind
  = TOOL` plus `tool.name` — which this demo currently cannot reach at all, and
  which no test outside the engine repository's own fixtures has ever fed.
- It would demonstrate genuinely zero-code instrumentation, where (a) still asks
  the operator to launch through a harness module.
- It would surface attributes a framework emits that we would not have thought
  to emit, including ones on the 22-attribute content deny-list — which is a
  test of the deny-list, not of the mapping.

What (b) would cost, and why not now: it replaces the application under test.
Every number in `docs/results/2026-09-27-stability.md` was measured against
*this* agent, and the comparison between the two sweeps is the point of running
a second one. Porting the agent changes the workload and the dependency set in
the same commit that changes the fidelity, and then a difference in the FAIL
rate has two candidate causes. It also adds a framework to
`agent/requirements.txt`, which is a decision about what this demo *is* and
should not ride along inside a measurement PR.

**Recommendation: (a) now, and (b) as its own later phase** — it is the more
valuable of the two for external users, and it deserves to be more than a
side effect.

### Where the wrapper lives — the brief's `tools/` will not work

The brief said "installed at import time from `tools/` (never from `agent/`)".
`agent/` is right and not negotiable. `tools/` is impossible, and the reason is
mechanical rather than stylistic:

```
$ .demo/tools-venv/bin/python -m pip list
Package Version
pip     26.2.1
PyYAML  6.0.2
$ .demo/tools-venv/bin/python -c "import requests"
ModuleNotFoundError: No module named 'requests'
```

`tools/` runs in its own interpreter, which holds PyYAML and nothing else. The
wrapper has to run in the agent's interpreter — it is the process whose spans
are being collected — and it must import both `opentelemetry` and `agent.tools`.
`tools/` can do neither. Putting it there and then adding `requests` and the
OpenTelemetry SDK to `tools-venv` would dissolve the separation that makes the
two-venv split mean anything.

So: a new top-level `harness/` package. Not under `agent/`, so the application
boundary is untouched; not under `tools/`, so the tooling boundary is untouched;
a third thing, which is what it actually is — it stands in for the
instrumentation a framework would have provided in option (b).

The invariant that matters is preserved exactly: the application under test
imports neither Trustvian nor OpenTelemetry. `harness/` is not the application.
`make smoke` should say so out loud rather than leaving it implied —
`APPLICATION_SOURCES` keeps its current contents, and a new assertion states
that `harness/` imports no Trustvian package and that nothing under `agent/` or
`fixtures/` imports `harness`.

The launch command changes from
`[opentelemetry-instrument, python, agent/main.py]` to
`[opentelemetry-instrument, python, harness/run_agent.py]` in the scenario
files. `agent/main.py` keeps working unchanged when run directly, with no tool
spans — which is itself worth a test.

## 2. The wider toolset

### Eight tools on the reference side

The three originals stay. Five are added. `export_customer` is **not** one of
the five: it is the candidate-only added behaviour the whole demo exists to
detect, and it must stay candidate-only. So the reference side has eight tools
and the candidate side nine.

| tool | method | host | path | argument field |
|---|---|---|---|---|
| `crm_lookup` | GET | `crm.localhost` | `/crm/customers/{id}` | `customer_id` |
| `knowledge_search` | GET | `knowledge.localhost` | `/knowledge/articles` | `query` |
| `send_email` | POST | `mail.localhost` | `/mail/send` | `email_subject`, `email_body` |
| `billing_lookup` | GET | `billing.localhost` | `/billing/invoices` | `customer_id` |
| `account_history` | GET | `history.localhost` | `/history/events` | `customer_id` |
| `escalate_ticket` | POST | `escalation.localhost` | `/escalations` | `reason` |
| `attach_diagnostics` | POST | `diagnostics.localhost` | `/diagnostics` | `customer_id` |
| `share_with_partner` | POST | `partner.localhost` | `/partner/share` | `customer_id` |
| `export_customer` *(candidate only)* | POST | `export.localhost` | `/export/customers` | `customer_id` |

Distinct hosts, not distinct paths on one host, matching the reason
`agent/tools.py` gives for the existing four: each backend is easy to tell apart
in logs. It also keeps the transport-level behaviour one-to-one with the
tool-level one, which § 3 needs.

**The response schema needs no new field.** `action_schema` already carries
exactly five scalars — `customer_id`, `query`, `email_subject`, `email_body`,
`reason` — and every new tool reads one of them. The `action` enum is already
built from `tool_names(mode)` (`agent/planner.py:105`), so it widens by itself.
This is worth stating because the alternative — one argument field per tool —
would have made the schema grow with the toolset and put thirteen required
scalars in front of a 4B model, and `action_schema`'s own docstring records what
happened last time a field was easy for the model to get wrong.

`escalate_ticket` reading `reason` is the one deliberate reuse: `reason` is
currently in the schema and read by nothing, which the docstring notes. Giving
it a consumer is a small improvement on its own.

### Tickets where the choice is real

Five tickets, replacing three. `SUPPORT_AGENT_ROUNDS` becomes 5, because
`main()` cycles `TICKETS[index % len(TICKETS)]` (`agent/main.py:211`) and at 3
rounds the last two tickets would never be visited.

| ticket | subject | needed | plausible but optional |
|---|---|---|---|
| t-1001 | Cannot sign in | `crm_lookup`, `knowledge_search`, `send_email` (3) | `attach_diagnostics` |
| t-1002 | Billing question | `crm_lookup`, `billing_lookup`, `send_email` (3) | `knowledge_search` |
| t-1003 | Feature request | `crm_lookup`, `send_email` (**2**) | `knowledge_search`, `escalate_ticket` |
| t-1004 | Repeated outages, considering cancelling | `crm_lookup`, `account_history`, `escalate_ticket`, `send_email` (**4**) | `attach_diagnostics` |
| t-1005 | Wants a copy of their data | `crm_lookup`, `account_history`, `escalate_ticket`, `send_email` (**4**) | `share_with_partner` |

Two tickets need two tools, two need four, and four tools are optional on at
least one ticket. `share_with_partner` is needed by no ticket and available in
both conditions — that is deliberate, and it is the shape
[`docs/design/2026-09-26-behavioral-regression-demo.md`](2026-09-26-behavioral-regression-demo.md)
Phase 4 already specified for it. It is the purest presence-variance probe in
the set: every time it appears, it appeared because the model decided to, and a
gate that counts it as an added behaviour is producing a false FAIL.

The prompt changes from prescribing a sequence — today `system_message` says
"Look up the customer record, find relevant help documentation, and send the
customer a useful reply" — to naming the ticket and asking for whichever tools
fit it. Without that change the toolset widens and the choice does not, and the
measurement would report the prompt's stability rather than the model's.

`MAX_STEPS` rises from 8 to 14, per ticket. A four-tool ticket plus `finish` is
five steps; with both optional tools and a refused action, nine. 14 leaves
headroom while still failing visibly on a model that never finishes, which is
the bound's stated purpose.

### The pass-through property is proven deterministically, not by comparison

Per 080: a fixture set including a success, an unknown tool, and an
argument-validation failure, run against the wrapped `dispatch` with no model,
asserting arguments by value and position, the return value, exceptions by name,
the call count (one in, one out — never retried, reordered, batched, cached or
suppressed), and the spans (exactly one `execute_tool` per dispatch, carrying
`gen_ai.tool.name`, carrying no argument, return-value or description
attribute).

Comparing model runs cannot prove this and must not be presented as proving it:
a wrapper that dropped every third call would still produce two similar-looking
behaviour sets. The deterministic test is the proof; `make smoke` stays
model-free and runs it.

## 3. What a behavior looks like now

This was not answerable on paper, so it was measured. A throwaway wrapper over
the *current* four-tool candidate mode, one real `gemma3:4b` run per side
against a real Ollama (server 0.34.4), through
`make scenario`, with the realtime SSE stream captured live from
`GET /v1/realtime`. Reproduced identically on two consecutive runs. The spike
was then deleted; it is not in this commit.

**Both layers appear, exactly as hoped:**

```
  REFERENCE observed 7 behaviors        CANDIDATE observed 9 behaviors
    crm_lookup ->               3         crm_lookup ->               3
    knowledge_search ->         3         export_customer ->          3
    send_email ->               3         knowledge_search ->         3
    GET   -> crm.localhost      3         send_email ->               3
    GET   -> knowledge.localhost 3        GET   -> crm.localhost      3
    POST  -> mail.localhost     3         POST  -> export.localhost   3
    POST  -> ollama.localhost  12         GET   -> knowledge.localhost 3
                                          POST  -> mail.localhost     3
                                          POST  -> ollama.localhost  15
  BEHAVIORAL DIFF: added 2
      + POST -> export.localhost
      + export_customer ->
```

69 observation frames: 21 tool, 21 HTTP to the mock services, 27 to
`ollama.localhost`. The wrapper emitted exactly one `execute_tool` span per
dispatch — 21 and 21, not 20 or 22 — which is the first evidence that the
pass-through does not drop or duplicate, though § 2's deterministic test is what
will actually prove it.

**`tool · export_customer` carries no target, and that is correct.**
`internal/semconv/genai.go`'s `opExecuteTool` branch sets `OperationCategory`
and `OperationName` and deliberately leaves `TargetName` unset; the adapter's
transport fallback finds no `server.address` on a span the wrapper created, so
the target is empty. The behaviour is `tool · export_customer · (no target)`
beside `http · POST · export.localhost`. The reference display renders the
empty target as trailing whitespace, which is cosmetic and worth one sentence
upstream, not a change here.

**Actor type stays `service`, not `ai_agent`.** `genAIActorType` upgrades the
actor only on an explicit `gen_ai.agent.name` or `gen_ai.agent.id`, and the
wrapper emits neither — by design, since 080's attribute list has two entries.
**This is a decision I would like made rather than assumed** (§ 5).

### Fidelity is rendered on the live view, and it reads `transport` on every record

This is the one thing that did not behave as expected, and it is an engine gap
rather than a demo one.

```
   6  cat='tool'  name='crm_lookup'       target=None  fidelity='transport'
   3  cat='tool'  name='export_customer'  target=None  fidelity='transport'
   6  cat='http'  name='GET'   target='crm.localhost'  fidelity='transport'
```

The mapping worked — category `tool`, the model-chosen name — so
`normalizeGenAI` set `FidelitySemantic` on its `Normalized`, and
`processor/mapping.go:124` wrote `trustvian.fidelity=semantic` onto the Event's
attributes. The value is then dropped on the way to the control plane:

- `platform/httpapi/dto.go:299` accepts `fidelity` on the ingest envelope,
  optional, absent meaning transport. The platform half is complete.
- `processor/internal/evaluation/client.go:255`'s `ingestEnvelope` has
  `version`, `sequence`, `behavioral_profile` and `record` — **and no
  `fidelity` field**. The Collector never sends it.
- `trustvian.DecisionRecord` has no `Attributes` field, so the value is not
  recoverable from the record either. It is simply not on the wire.
- `platform/webui/assets/live.js:230` and `:252` read `observation.fidelity ||
  "transport"`, and `inspector.js:76` states a sentence when it is `semantic`.
  The view is ready and is being told `transport`.

So fidelity **is** reported on the live view; through the Collector ingest path
it always reports `transport`, including for records whose identity came from a
semantic convention. Fidelity reaches the live view today only for a producer
that POSTs to the ingest endpoint directly and fills the field itself.

The fix is small and belongs upstream: `fidelity` rides beside the record in the
processor's envelope, exactly as `behavioral_profile` already does and for the
identical stated reason. The sink's `Record(ctx, record, learning)` signature
takes a `DecisionRecord`, which carries no attributes, so the value has to be
passed alongside — the processor has the `Result` and can read
`result.Event.Attributes[event.AttrFidelity]` at the call site
(`processor/processor.go:528`). This goes in `docs/upstream/` as a proposal; I
will not touch `../trustvian`.

**This does not block the sweep.** The category and the name are what a
behaviour is keyed on and what a gate counts, and both are correct. Fidelity is
the indicator that explains *why* a name is a tool name; it is missing from the
UI, not from the mapping.

### The scenario gates on the tool-level behaviors

Measured reason, not a preference. One behavioural change — the candidate
exporting a customer record — produced **two** added behaviours, because it is
visible at both layers:

```
    added   2
      + POST -> export.localhost
      + export_customer ->
```

Gating on both layers double-counts every regression. At
`max_added_behaviors: 0` that is invisible, which is exactly why it is worth
fixing before the budget is ever nonzero: a suite that allows one added
behaviour would be allowing half of one, and the operator who set the budget
would have no way to know.

Beyond the double count, the tool layer is the better gate on its own terms:

- **It is what the model chose.** `export.localhost` is written in
  `agent/tools.py`; `export_customer` is what came back from the planner. A gate
  on the transport layer is a gate on the dispatcher's routing table, which
  cannot change without a code change — it can only ever detect what a diff
  would already have shown.
- **It is where presence variance can exist.** Condition 1 of 078's re-run is
  about a model visiting different tools on different runs. That variance lives
  in the tool layer by construction.
- **It survives refactors that should not be regressions.** Moving
  `export_customer` from `export.localhost` to `audit.localhost/export` is an
  added *and* removed transport behaviour and no change at all at the tool
  layer. The second reading is the true one.

The HTTP behaviours stay collected and visible — they are the evidence that a
tool call reached a real service, and 075's whole claim is that both readings
coexist. They are reported and not gated. The `ollama.localhost` records stay
outside the gate as well: 12 vs 15 observations across two identical
configurations is turn-count noise, which is precisely the false-FAIL source
this measurement exists to quantify.

## 4. The measurement plan

`make stability RUNS=10 TEMPERATURE=0.7`, unchanged in shape from the first
sweep so the two are comparable, in both configurations:

- **shared profile** — every repetition in one behavioural profile, so baselines
  and anomaly confidence accumulate across runs
- **isolated profiles** — a fresh profile per repetition

at tool fidelity, with the eight/nine-tool toolset and the five tickets. The
gate counts tool-level behaviours only.

Outputs, the same as
[`docs/results/2026-09-27-stability.md`](../results/2026-09-27-stability.md):

- adjacent-pair and all-pairs gate FAIL rates, with added-behaviour failures
  separated from block and critical-risk failures
- per-behaviour "seen in k/N", under the heading **demo-side aggregation — what
  a `runs: N` gate would see**, labelled as demo-side because the control plane
  does not aggregate across runs
- per run: `record_count`, `distinct_behavior_count`, anomaly confidence
- every verdict from the control plane; no verdict computed here

plus the new one:

- **the per-tool presence table** — for each of the eight reference tools, in
  how many of the 10 runs it was dispatched at least once. This is the table the
  first sweep could not have produced: with three mandatory tools it would have
  been 10/10 three times over, which is what it was.

And the answer to the question 078 actually needs: **the smallest `k` that would
have produced zero false FAILs**, where `k` is the minimum number of runs a
behaviour must appear in to count as present. If `share_with_partner` shows up
in 6 of 10 runs, a set-semantics gate (`k = 1`) fails whenever the two sides
disagree, and `k` has to come down far enough to absorb it. If every tool is
still 10/10, `k = 1` suffices and **that is the result** — it says the
nondeterminism is in turn counts and not in tool selection, and 078's `k`/`j`
guidance should say so rather than hedging.

`make stability` reads behaviours by self-compare today. It will switch to
`GET /v1/evaluation-runs/{run_id}/behaviors` when 078 ships it — that route is
078's first implementation slice, and it is the reason the demo-side aggregation
has to be labelled as such until then.

### What could make this sweep report nothing

Stated in advance so a null result is not quietly reinterpreted:

- **A 4B model may not exercise the choice.** `gemma3:4b` with a closed enum and
  a required-scalar schema may converge on the same tool sequence every time
  regardless of temperature. Then the presence table is all 10/10 again, and the
  honest report is "a wider toolset did not produce presence variance at this
  model size", with the per-tool data shown. That is a finding about what 078's
  `runs: N` buys, not a failed measurement.
- **A concurrent `make smoke` destroys a sweep.** `start_runtime` stops any
  runtime serving this directory's `.trustvian/` — it did exactly that to the
  first T=1.3 sweep at repetition 19, and it did it again to a capture while
  this document was being written. The sweep correctly refuses to publish a
  partial rate (exit 3). Nothing else may run in this directory during a sweep.
- **Ollama can be up and unable to answer.** While preparing this, the HTTP port
  was listening and `/api/version` answered 200, while a `llama-server` model
  runner sat in process state `T` — stopped. PR #8 is the gate for exactly that
  and is still open; it should land before the sweep runs.

## 5. Decisions I would like from you

1. **`harness/` as a third top-level directory**, since the brief's `tools/` is
   mechanically impossible (§ 1). If you would rather it live somewhere else,
   the only hard requirement is that it be importable by the agent's
   interpreter and never imported by `agent/`.
2. **Whether the wrapper also emits `gen_ai.agent.name`.** 080 lists two
   attributes and the spike emitted two, so the actor type stays `service`.
   Adding it would make the tool records `ai_agent` — which is what they are,
   and which would exercise ADR 0014's actor typing and `genAIActorType`. The
   cost is real and worth knowing: `ActorType` is one of the six
   `StableFeatures` dimensions, so the tool records and the HTTP records of the
   same tool call would carry different actor types and different fingerprints,
   and the current 27-record reference baseline would not carry over. I lean
   **no for this PR** — it changes the fingerprints in the same commit that
   changes the fidelity, and the sweep wants one variable — and **yes as its own
   small follow-up**.
3. **Five new tickets replacing three**, with `SUPPORT_AGENT_ROUNDS` going 3 → 5
   and `MAX_STEPS` 8 → 14. This changes every record count documented in
   `README.md`, including the 27 that `make smoke` asserts. Those are
   re-measured and updated, not deleted — but it is a visible change to the
   numbers the README teaches, so I would rather you knew before I made it.
4. **Whether the upstream fidelity gap (§ 3) should be a proposal or a
   pull request against `../trustvian`.** My instruction is never to modify that
   checkout, so the default is a document under `docs/upstream/`. It is a small,
   well-understood fix, and the indicator task 075 shipped does not currently
   reach any operator through the Collector path, so it may deserve more than a
   note.

# Task 078 — the measurement, and what it changes

**A proposal for [`trustvian`](../../../trustvian), not a change to it.** Nothing
in this repository modifies that checkout;
[the design doc](../design/2026-09-26-behavioral-regression-demo.md)'s invariant 4
makes a gap that needs Trustvian work become a document here instead.

Addressed to
[task 078 § *Measurement before implementation*](../../../trustvian/docs/tasks/v1.0/078-behavioral-scenario-suites.md),
which asks for four things before the task is built. Measured against Trustvian
`5362f51`; conditions and raw numbers in
[docs/results/2026-09-27-stability.md](../results/2026-09-27-stability.md).

## The four things 078 asked for

### 1. A real, instrumented, nondeterministic agent, run against itself 10+ times

Done: `agent/main.py`'s reference side, Ollama `gemma3:4b` at temperature 0.7,
N = 10, in two learning configurations.

### 2. The false-FAIL rate under the current task 056 gates at N = 1

**Zero.** 0 of 9 adjacent pairs, 0 of 90 ordered pairs, in both configurations —
and **still zero at temperature 1.3**, in a second sweep of the same size run
specifically to test whether 0.7 was the reason.

### 3. The rate under the proposed gates at N ≥ 5

**Not measurable, because there is nothing for them to change.** Every behavior
appeared in 10 of 10 runs, so no k — including `k = N` — alters any verdict. A
k-of-N rule can only suppress a finding that presence variance produced, and
there was none.

### 4. Whether checks 5 and 6 are usable against a freshly allocated scope

**No. They are inert.** With one scope per repetition, anomaly confidence is
0.1786 in every repetition — a fresh scope has learned nothing, so trust is
barely penalized, so no `BLOCK` is decided and no critical-risk observation is
recorded. Both checks read `0` in all 90 comparisons.

With one shared scope, confidence climbs monotonically to saturation across ten
repetitions (0.1786 → 1.0000, reaching 1.0000 by repetition 8). The checks become
live, but only after warming, and the warming is a function of repetition index.

## What this does and does not establish

**It does not refute the amendment in general.** It refutes it for the one
workload 078 named, and the reason is worth more than the number.

078 selected this workload because "its action order is the model's choice and
differs between runs". Action order does differ — record counts varied between
runs (21, 22, 23), so the agent took different numbers of turns. But **behavioral
identity is deliberately not sequence-dependent**, so order variance produces no
diff variance at all. The property the workload was selected for is precisely the
one the diff ignores.

What would produce presence variance is a run that *reaches a different set of
services*, and this workload structurally cannot: at HTTP fidelity a behavior is
a method and a destination, the reference toolset has three tools on three hosts,
the model call is the fourth, and the prompt requires all three actions for each
of three tickets. A behavior could only go missing if the model skipped one tool
for every ticket in a run.

**The second sweep is what turns that from an argument into a measurement.** At
temperature 1.3 the agent became visibly less deterministic — four of ten runs
took two to four extra turns, against one of ten at 0.7 — and the behavior set was
four in every one of the forty model-driven runs across both sweeps, with the FAIL
rate 0/9 and 0/90 in all four configuration-temperature combinations. Turn-count
nondeterminism nearly doubled and the gate did not notice, because the gate reads
the behavior set and the behavior set was saturated.

**So the honest conclusion is narrower than either side of 078's question:**

- the k-of-N machinery is **not justified by this measurement**;
- this measurement **does not qualify** as the measurement 078 needs, because the
  workload it names cannot exhibit the phenomenon;
- 078's *"the repository's own evidence points the other way"* is now contradicted
  by a number, and the section should be reconsidered on that basis.

## Recommendation

### On N and k

**Do not ship a default k.** There is no measured basis for one, and 078 is right
that "a guessed threshold that ships becomes the contract".

**`runs: N` is still worth having**, for a reason this measurement supports
rather than undermines: N repetitions with per-repetition isolation is what makes
the *absence* of variance demonstrable. This sweep's value is that it can say
"10/10, every behavior, both configurations" — which a single run cannot.

**Suggested shape:** `runs: N` with presence counts always reported, and `k`/`j`
**required when set and absent by default**, so a scenario that wants k-of-N
states it and one that does not gets plain set semantics. That matches task 056's
existing rule that zero is a strict limit and never a default.

### On what to measure before committing to k

A workload whose behavioral surface is **larger than what one run visits**. Three
candidates, cheapest first:

1. **More tools than the prompt requires per ticket** — say eight tools with an
   instruction to use whichever fit. Then which services a run reaches is a real
   choice, and presence counts vary for the reason 078 cares about.
2. **A larger model.** `gemma3:4b` at 0.7 followed this three-step workflow
   essentially perfectly. A model with more freedom in tool selection would vary
   more, and this measurement says nothing about that.
3. **Task 075's semantic fidelity.** At tool-name fidelity a behavior is
   `export_customer`, not `POST → export.localhost`, so the behavioral surface
   grows to the size of the toolset and stops being saturated by a three-step
   workflow. **This is the most important one**: it may change the answer to 078's
   whole question, because the phenomenon k-of-N exists to absorb might only
   appear at the fidelity 075 delivers.

This repository will run the same sweep against (1) when Phase 4's larger toolset
exists, and against (3) when 075 lands, and publish both.

### On the tension between isolation and checks 5 and 6

078 leaves the warming question open and says the measurement should answer it.
It now has, and the answer is that **the two requirements conflict**:

- per-repetition isolation is needed so presence counts measure nondeterminism
  rather than learning order;
- checks 5 and 6 need a warmed baseline to mean anything, and a fresh scope makes
  them structurally unable to fire.

A scenario cannot have both from one set of repetitions. Three options, with a
recommendation:

| | Consequence |
|---|---|
| Warm one shared profile before the measured repetitions, isolate only those | Two phases per side; the warming runs produce evidence nobody counts, and how much warming is enough becomes another guessed constant |
| Keep isolation and **state that checks 5 and 6 are advisory** at `N > 1` | Honest, costs nothing, and matches what they actually do against a fresh scope |
| Keep isolation and drop the two checks from scenario gates | Loses a real signal for deployments that do warm a profile |

**Recommended: the second.** Say in the specification that against freshly
allocated scopes the block-decision and critical-risk checks report `0` by
construction, so a scenario setting them to `0` is asserting something that
cannot fail. That is a documentation change rather than a mechanism, and it stops
a developer reading two passing checks as evidence.

## The interim route 078's aggregation needs

078's [result document](../../../trustvian/docs/tasks/v1.0/078-behavioral-scenario-suites.md#result-document)
specifies per-behavior `reference_runs_present` and `candidate_runs_present`
computed by the control plane. **Once that exists, the trick below is
unnecessary** — the counts come from the platform and no consumer aggregates
anything.

Until then there is no per-run behavior snapshot on `/v1`.
`GET /v1/evaluation-runs/{id}/progress` returns counts only, and
`POST /v1/evaluations/compare` is the only route returning behaviors. So this
repository reads a run's behavior set by **comparing the run against itself**,
which `CompareEvaluations` permits — the same-run refusal exists only for
promotions. Every delta comes back `shared`, and that list *is* the run's
behavior set, computed by the server.

It works because nothing forbids it, not because it is a contract. **A
run-scoped behavior route is still worth adding**, and it is worth adding
*before* the full aggregation, because:

- the self-compare costs N extra comparisons per side, each computing a full
  scorecard and gate nobody reads;
- it is undocumented behavior a same-run guard would silently break — this
  repository detects that case and refuses rather than reporting a wrong set, but
  a consumer that did not would report confidently wrong counts;
- 079 and 080 both want per-run behavior before 078's aggregation ships.

Suggested shape, reusing decisions that already exist:

```text
GET /v1/evaluation-runs/{run_id}/behaviors
    → the run's behavior set: fingerprint id, descriptor, observation count
    → bounded and paged exactly as task 065's collections are
      (id byte-ascending, exclusive "after" cursor, limit 1–64)
    → 404 for a missing run; 200 with an empty array for one with no evidence
```

No new semantics: it publishes what a self-compare already returns, without the
scorecard and gate nobody asked for.

## Method, so a reader can check it

```bash
make stability RUNS=10 TEMPERATURE=0.7
```

Reference side only, N repetitions, each a separate `trustvian dev` invocation
with an explicit `--candidate`. Two configurations — one shared learning scope,
and one per repetition. Every FAIL is a real `eval compare`; every behavior set
came back from one; the k/N counting counts responses and is labelled
demo-side aggregation wherever it is printed.

Each sweep allocates a fresh candidate namespace, because `trustvian dev` keeps
the learned baseline in a file per candidate and it persists across invocations.
Without that, a sweep would start from whatever a previous sweep taught it — and
the shared-versus-isolated comparison above would be meaningless.

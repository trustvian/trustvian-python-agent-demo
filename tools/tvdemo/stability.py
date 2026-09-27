"""How often an unchanged agent fails a single-run gate against itself.

This is the measurement Trustvian task 078 requires *before* it is implemented:
its own "Measurement before implementation" section says `k`, `j` and a default
`N` are guesses until somebody measures a real agent, and that the measurement is
allowed to refute the amendment.

So this module measures and reports. It decides nothing, and it computes no
verdict: every FAIL below is a control-plane response to a real
`POST /v1/evaluations/compare`, and every behavior count is read out of one.
Where it aggregates — counting how many of N runs showed a behavior — it counts
responses and says so under a heading that names it as demo-side aggregation.

Two configurations, because they answer different questions
------------------------------------------------------------

**shared** — all N repetitions under one candidate id, which is one learning
scope. This is what a developer gets by default, and what `trustvian dev` does if
you run it N times with the same `--candidate`. Repetition i is analyzed against
a baseline that has already learned from 1..i-1, so novelty, anomaly confidence
and every sequence signal are a function of repetition index.

**isolated** — a candidate id per repetition, which is what task 078 specifies
for scenario suites, precisely so the presence counts measure the workload's
nondeterminism rather than the order the repetitions ran in.

Neither is a default the other inherits. Reporting only one would answer only
half of what 078 has to decide.

A fresh candidate namespace per sweep
-------------------------------------

Both configurations allocate candidate ids containing this sweep's own
identifier, so every profile starts from an empty baseline.

That is not tidiness. `trustvian dev` keeps the engine's learned baseline in a
file under its state directory, one per candidate, and it **persists across
invocations** — measured: repeated runs under one candidate drove anomaly
confidence to its maximum. A sweep reusing a candidate id would therefore start
from whatever previous sweeps taught it, and the "shared versus isolated"
difference would be contaminated by history nobody recorded. Allocating fresh
ids gets a clean start without deleting anything dev owns.
"""

from __future__ import annotations

import datetime
import itertools
import json
import os
import pathlib
import subprocess

from . import world

# The configurations, in report order.
SHARED = "shared"
ISOLATED = "isolated"
CONFIGURATIONS = (SHARED, ISOLATED)

# The three gate checks a comparison can fail on for behavioral reasons, plus
# the two evidence checks. Reported separately because they fail for unrelated
# causes: added behaviors are the workload's nondeterminism, while block
# decisions and critical-risk observations are the engine's reading of it against
# a baseline — and 078 has to know which of those its k-of-N machinery absorbs.
BEHAVIOR_CHECK = "added_behaviors"
ENGINE_CHECKS = ("block_decisions", "critical_risk_observations")
EVIDENCE_CHECKS = ("reference_evidence", "candidate_evidence")


class Sweep:
    """N repetitions of one unchanged side, compared against itself."""

    def __init__(self, spec, root, plane, api_url, runs, model, temperature,
                 mock_port=None, stream=False, namespace=None):
        self.spec = spec
        self.root = pathlib.Path(root)
        self.plane = plane
        self.api_url = api_url
        self.runs = runs
        self.model = model
        self.temperature = temperature
        self.mock_port = mock_port
        self.stream = stream
        self.bin_dir = self.root / ".demo" / "bin"
        self.venv = self.root / ".demo" / "venv"
        self.namespace = namespace or datetime.datetime.now(
            datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")

    # -- identity ------------------------------------------------------

    def candidate_id(self, configuration, repetition):
        """The learning scope one repetition runs under.

        The candidate *is* the learning scope, so this one function is the whole
        difference between the two configurations.
        """
        base = f"stab-{self.namespace}-{configuration}"
        if configuration == ISOLATED:
            return f"{base}-rep-{repetition}"
        return base

    def run_id(self, configuration, repetition):
        return f"stab-{self.namespace}-{configuration}-{repetition}"

    # -- one repetition ------------------------------------------------

    def _invoke(self, configuration, repetition):
        """One evaluation run of the unchanged reference side, through dev."""
        side = self.spec.reference
        run_id = self.run_id(configuration, repetition)

        command = [
            str(self.bin_dir / "trustvian"), "dev",
            "--api-url", self.api_url,
            "--project", "support-demo",
            "--agent", "support-agent",
            "--environment", "local",
            "--candidate", self.candidate_id(configuration, repetition),
            "--run-id", run_id,
            "--instrumentation", self.spec.instrumentation,
            "--",
        ] + [self._resolve(token) for token in side.command]

        child_env = dict(os.environ)
        child_env.update(side.env)
        if self.mock_port is not None:
            child_env["SUPPORT_AGENT_PORT"] = str(self.mock_port)
        child_env["OLLAMA_MODEL"] = self.model
        # The measurement's whole point. Stated on every repetition rather than
        # assumed from a default, and recorded in the report beside the numbers.
        child_env["OLLAMA_TEMPERATURE"] = str(self.temperature)
        # A distinct seed per repetition, for the simulated workload only. The
        # model-driven one ignores it.
        child_env["SUPPORT_AGENT_SEED"] = f"{self.namespace}-{repetition}"
        child_env.setdefault("PYTHONPATH", str(self.root))
        child_env["TRUSTVIAN_LOCAL_BIN"] = str(self.bin_dir / "trustvian-local")
        child_env["TRUSTVIAN_COLLECTOR_BIN"] = str(self.bin_dir / "trustvian-collector")

        output = None if self.stream else subprocess.DEVNULL
        completed = subprocess.run(command, cwd=str(self.root), env=child_env,
                                   stdout=output, stderr=None)
        if completed.returncode != 0:
            raise world.WorldError(
                f"repetition {repetition} ({configuration}) exited "
                f"{completed.returncode} under trustvian dev; run {run_id} was "
                f"failed, not completed. Nothing downstream may read it as "
                f"evidence, so the sweep stops rather than reporting a rate over "
                f"a smaller N than it claims.")
        return run_id

    def _resolve(self, token):
        if token == "python":
            return str(self.venv / "bin" / "python")
        if token == "opentelemetry-instrument":
            return str(self.venv / "bin" / "opentelemetry-instrument")
        return token

    # -- the sweep -----------------------------------------------------

    def run(self) -> dict:
        report = {
            "namespace": self.namespace,
            "runs": self.runs,
            "model": self.model,
            "temperature": self.temperature,
            "scenario": self.spec.name,
            "workload": list(self.spec.reference.command),
            "gate": dict(self.spec.gate),
            "configurations": {},
        }
        for configuration in CONFIGURATIONS:
            print(f"\n=== configuration: {configuration} "
                  f"({self._describe(configuration)}) ===", flush=True)
            report["configurations"][configuration] = \
                self._one_configuration(configuration)
        return report

    def _describe(self, configuration):
        if configuration == SHARED:
            return "one learning scope for all repetitions — the default"
        return "one learning scope per repetition — task 078's specified shape"

    def _one_configuration(self, configuration) -> dict:
        run_ids = []
        for repetition in range(1, self.runs + 1):
            print(f"  repetition {repetition}/{self.runs} "
                  f"(candidate {self.candidate_id(configuration, repetition)})",
                  flush=True)
            run_ids.append(self._invoke(configuration, repetition))

        # Per run: what the control plane holds, and how the engine read it.
        per_run = []
        for run_id in run_ids:
            progress = self.plane.progress(run_id)
            # Self-compare, which is also where the behavior set comes from: the
            # /v1 API exposes no per-run behavior snapshot, and
            # POST /v1/evaluations/compare is the only route that returns
            # behaviors at all.
            _, payload = self.plane.compare(run_id, run_id, self.spec.gate)
            confidence = payload["scorecard"]["metrics"]["anomaly_confidence"]
            per_run.append({
                "run_id": run_id,
                "record_count": int(progress["record_count"]),
                "distinct_behavior_count": int(progress["distinct_behavior_count"]),
                "anomaly_confidence_mean":
                    confidence["candidate"]["mean"]
                    if confidence["candidate"]["mean_known"] else None,
                "behaviors": self.plane.behaviors_of(run_id, self.spec.gate),
            })

        return {
            "candidates": sorted({self.candidate_id(configuration, i)
                                  for i in range(1, self.runs + 1)}),
            "per_run": per_run,
            "adjacent_pairs": self._pairs(
                list(zip(run_ids, run_ids[1:]))),
            "all_pairs": self._pairs(
                [(a, b) for a, b in itertools.permutations(run_ids, 2)]),
            "presence": self._presence(per_run),
        }

    def _pairs(self, pairs) -> dict:
        """Every comparison is a real gate result from the control plane."""
        results = []
        for reference, candidate in pairs:
            status, payload = self.plane.compare(
                reference, candidate, self.spec.gate)
            gate = payload["gate"]
            failed = [name for name in
                      (BEHAVIOR_CHECK, *ENGINE_CHECKS, *EVIDENCE_CHECKS)
                      if not gate[name]["passed"]]
            results.append({
                "reference_run": reference,
                "candidate_run": candidate,
                "exit_code": status,
                "verdict": gate["verdict"],
                "failed_checks": failed,
                "added_count": int(payload["behavior_diff"]["added_count"]),
                "removed_count": int(payload["behavior_diff"]["removed_count"]),
            })

        total = len(results)
        failing = [r for r in results if r["verdict"] == "fail"]
        return {
            "total": total,
            "failed": len(failing),
            "fail_rate": (len(failing) / total) if total else None,
            # Split out, because these fail for unrelated reasons and 078 has to
            # know which of them k-of-N presence counting can absorb.
            "failed_on_added_behaviors": sum(
                1 for r in results if BEHAVIOR_CHECK in r["failed_checks"]),
            "failed_on_engine_checks": sum(
                1 for r in results
                if any(c in r["failed_checks"] for c in ENGINE_CHECKS)),
            "failed_on_evidence_checks": sum(
                1 for r in results
                if any(c in r["failed_checks"] for c in EVIDENCE_CHECKS)),
            "comparisons": results,
        }

    def _presence(self, per_run) -> list:
        """In how many of the N runs did each behavior appear.

        Presence counting over control-plane responses. It reimplements no diff:
        each run's behavior set came back from a comparison, and this counts how
        many of those sets a fingerprint appears in.
        """
        seen = {}
        for entry in per_run:
            for behavior in entry["behaviors"]:
                key = behavior["fingerprint_id"]
                record = seen.setdefault(key, {
                    "fingerprint_id": key,
                    "operation_name": behavior["operation_name"],
                    "target_name": behavior["target_name"],
                    "runs_present": 0,
                    "observations_total": 0,
                })
                record["runs_present"] += 1
                record["observations_total"] += behavior["observations"]
        return sorted(seen.values(),
                      key=lambda r: (-r["runs_present"], r["target_name"],
                                     r["operation_name"]))


def zero_false_fail_threshold(presence, runs):
    """The smallest k for which no behavior would cross a k-of-N threshold.

    An unchanged agent compared against itself should produce no finding, so a
    k-of-N rule is only useful if some k exists at which this sweep reports
    nothing. That k is one more than the highest presence count among the
    behaviors that were *not* present in every run — a behavior present in all N
    is not noise, it is what the agent always does.

    Returns None when every behavior appeared in every run, which means the
    single-run gate already had nothing to absorb and the machinery is not
    justified by this measurement. 078 says that outcome must be reportable.
    """
    varying = [p["runs_present"] for p in presence if p["runs_present"] < runs]
    if not varying:
        return None
    return max(varying) + 1


def render(report) -> str:
    """The human-readable report. Every number came from a response."""
    out = []
    add = out.append
    add(f"stability sweep {report['namespace']}")
    add(f"  scenario     {report['scenario']}")
    add(f"  workload     {' '.join(pathlib.Path(t).name for t in report['workload'])}")
    add(f"  runs (N)     {report['runs']} per configuration")
    add(f"  model        {report['model']}")
    add(f"  temperature  {report['temperature']}")
    add(f"  gate         " + ", ".join(f"{k} = {v}"
                                       for k, v in report["gate"].items()))

    for configuration, data in report["configurations"].items():
        add("")
        add(f"configuration: {configuration}")
        add(f"  learning scopes: {len(data['candidates'])} "
            f"({'one, shared' if len(data['candidates']) == 1 else 'one per repetition'})")
        add("")
        add("  per run, from the control plane")
        add("    run                              records  behaviors  anomaly confidence")
        for entry in data["per_run"]:
            confidence = ("      —" if entry["anomaly_confidence_mean"] is None
                          else f"{entry['anomaly_confidence_mean']:>7.4f}")
            add(f"    {entry['run_id']:<32} {entry['record_count']:>7}  "
                f"{entry['distinct_behavior_count']:>9}  {confidence}")

        add("")
        add("  single-run gate FAIL rate, from eval compare")
        for label, key in (("adjacent pairs (i vs i+1)", "adjacent_pairs"),
                           ("all ordered pairs", "all_pairs")):
            pairs = data[key]
            rate = ("n/a" if pairs["fail_rate"] is None
                    else f"{pairs['fail_rate'] * 100:.1f}%")
            add(f"    {label:<28} {pairs['failed']:>4} / {pairs['total']:<4} "
                f"FAIL   {rate}")
            add(f"      of which added_behaviors          "
                f"{pairs['failed_on_added_behaviors']}")
            add(f"      of which block/critical-risk      "
                f"{pairs['failed_on_engine_checks']}")
            add(f"      of which minimum-evidence         "
                f"{pairs['failed_on_evidence_checks']}")

        add("")
        add("  demo-side aggregation — what a `runs: N` gate would see")
        add(f"    counted from {report['runs']} self-compare responses; the")
        add("    control plane computed each behavior set, this counted presence")
        add("    behavior                                  seen in")
        for entry in data["presence"]:
            behavior = f"{entry['operation_name']} -> {entry['target_name']}"
            add(f"    {behavior:<40}  {entry['runs_present']}/{report['runs']}")
        k = zero_false_fail_threshold(data["presence"], report["runs"])
        if k is None:
            add("")
            add("    every behavior appeared in every run: at this N and this")
            add("    temperature there was no presence variance for a k-of-N rule")
            add("    to absorb.")
        else:
            add("")
            add(f"    k = {k} of {report['runs']} would have produced no finding here")

    return "\n".join(out)


def write(report, path):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2))
    return path

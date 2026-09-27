"""Execute one scenario and report what the control plane said about it.

The runner is an adapter, in the sense ADR 0023 and ADR 0033 use and task 078
repeats: it drives the control plane over its existing surface and decides
nothing. There is no diff here, no scorecard, no gate, no policy outcome and
no ordering comparison. Every verdict below is read out of a response.

Exit codes follow the contract that already exists:

    0   gate PASS
    1   gate FAIL
    2   usage — a malformed scenario, reported before anything runs
    3   operational — the API, the network, or the workload failing to run

A workload that crashes is 3, never 1. A broken test run is not a behavioral
regression, and conflating them would make every CI failure ambiguous.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

from . import controlplane, world

# The same identifiers scripts/lib.sh uses, so a scenario run and `make demo`
# land in one hierarchy rather than two that look alike.
PROJECT_ID = "support-demo"
AGENT_ID = "support-agent"
ENVIRONMENT_REF = "local"

EXIT_PASS = 0
EXIT_GATE_FAIL = 1
EXIT_USAGE = 2
EXIT_OPERATIONAL = 3


class Runner:
    def __init__(self, scenario, root, api_url=None, model="gemma3:4b",
                 stream=False):
        self.scenario = scenario
        self.root = pathlib.Path(root)
        self.api_url = api_url
        self.model = model
        self.stream = stream
        self.bin_dir = self.root / ".demo" / "bin"
        self.venv_python = self.root / ".demo" / "venv" / "bin" / "python"
        self.results = []

    # -- one repetition of one side ------------------------------------

    def _invoke(self, side, repetition, mock_port, api_url):
        """One evaluation run, through `trustvian dev`.

        Every identity value is explicit. `--candidate` in particular: both
        sides of a comparison are usually built from the same commit, and dev
        derives the candidate from the commit, so leaving it out would give
        them one candidate id and therefore one learning profile — the
        candidate *is* the learning scope.

        `--api-url` attaches to the control plane the caller started. N
        repetitions against N control planes would be N databases with nothing
        to compare across.
        """
        run_id = side.run_id(self.scenario.name, repetition)
        summary = self.root / ".runtime" / f"{run_id}-summary.json"

        command = [
            str(self.bin_dir / "trustvian"), "dev",
            "--api-url", api_url,
            "--project", PROJECT_ID,
            "--agent", AGENT_ID,
            "--environment", ENVIRONMENT_REF,
            "--candidate", side.candidate_for(repetition),
            "--run-id", run_id,
            "--instrumentation", self.scenario.instrumentation,
            "--",
        ] + [self._resolve(token) for token in side.command]

        environment = dict(**side.env)
        if mock_port is not None:
            environment["SUPPORT_AGENT_PORT"] = str(mock_port)
        environment["OLLAMA_MODEL"] = self.model
        environment["SUPPORT_AGENT_SUMMARY"] = str(summary)

        import os
        child_env = dict(os.environ)
        child_env.update(environment)
        child_env.setdefault("PYTHONPATH", str(self.root))
        # dev resolves its two helpers flag -> environment -> beside the
        # executable -> PATH, and this repository builds them somewhere that is
        # none of those.
        child_env["TRUSTVIAN_LOCAL_BIN"] = str(self.bin_dir / "trustvian-local")
        child_env["TRUSTVIAN_COLLECTOR_BIN"] = str(self.bin_dir / "trustvian-collector")

        output = None if self.stream else subprocess.DEVNULL
        completed = subprocess.run(
            command, cwd=side.workdir or str(self.root), env=child_env,
            stdout=output, stderr=None)
        if completed.returncode != 0:
            raise world.WorldError(
                f"the {side.name} workload (repetition {repetition}) exited "
                f"{completed.returncode} under trustvian dev; run {run_id} was "
                f"failed, not completed")
        return run_id

    def _resolve(self, token):
        """Resolve the two tokens a scenario is allowed to name abstractly.

        `python` is the demo-managed interpreter and
        `opentelemetry-instrument` the zero-code launcher beside it. Spelled out
        rather than left to PATH: which Python runs decides which OpenTelemetry
        runtime is attached, and inheriting whatever happens to be first on PATH
        is how a scenario silently stops being instrumented.

        The wrapper is part of the scenario's command rather than something the
        runner prepends, because that is what `--instrumentation existing` then
        truthfully describes — dev configures OTLP and injects nothing, and the
        command says where the instrumentation comes from.
        """
        if token == "python":
            return str(self.venv_python)
        if token == "opentelemetry-instrument":
            return str(self.root / ".demo" / "venv" / "bin" / "opentelemetry-instrument")
        return token

    def _assert_evidence(self, plane, run_id, side, repetition):
        """Check the run holds the evidence the scenario predicted.

        dev has no evidence wait and needs none: it stops the Collector before
        moving the run to a terminal state, which flushes what the Collector
        holds. So a predicted count is checked *afterwards*, as an equality —
        stronger than the lower-bound wait this repository used before, which
        could not notice a workload that did more than expected.
        """
        if side.records is None:
            return
        progress = plane.progress(run_id)
        actual = int(progress["record_count"])
        if actual != side.records:
            raise world.WorldError(
                f"run {run_id} ({side.name}, repetition {repetition}) holds "
                f"{actual} records, and the scenario predicted {side.records}. "
                f"Its status is {progress['status']!r}.")

    # -- the whole scenario --------------------------------------------

    def run(self) -> int:
        scenario = self.scenario
        if scenario.needs_a_model:
            world.require_ollama(self.model)

        with world.Runtime(self.root, self.api_url) as runtime:
            plane = controlplane.ControlPlane(self.bin_dir / "trustvian",
                                              runtime.api_url)
            mocks = None
            if "mocks" in scenario.services:
                mocks = world.Mocks(self.root, self.venv_python)

            context = mocks if mocks is not None else _nothing()
            with context:
                port = mocks.port if mocks is not None else None
                return self._compare_every_repetition(plane, port, runtime.api_url)

    def _compare_every_repetition(self, plane, port, api_url) -> int:
        scenario = self.scenario
        worst = EXIT_PASS

        for repetition in range(1, scenario.runs + 1):
            if scenario.runs > 1:
                print(f"\nrepetition {repetition} of {scenario.runs}")
            reference = self._invoke(scenario.reference, repetition, port, api_url)
            self._assert_evidence(plane, reference, scenario.reference, repetition)
            candidate = self._invoke(scenario.candidate, repetition, port, api_url)
            self._assert_evidence(plane, candidate, scenario.candidate, repetition)

            status, payload = plane.compare(reference, candidate, scenario.gate)

            # Each side's own behavior set, read back from the control plane
            # by comparing the run against itself. Printed here because it is
            # what a `runs: N` view aggregates over, and kept in the results
            # file for the same reason.
            observed = {
                "reference": plane.behaviors_of(reference, scenario.gate),
                "candidate": plane.behaviors_of(candidate, scenario.gate),
            }

            self.results.append({
                "repetition": repetition,
                "reference_run": reference,
                "candidate_run": candidate,
                "exit_code": status,
                "comparison": payload,
                "observed": observed,
            })
            worst = max(worst, status)
            self._report(payload, status, observed)

        return worst

    def _report(self, payload, status, observed):
        diff = payload["behavior_diff"]
        gate = payload["gate"]
        print()
        for side in ("reference", "candidate"):
            behaviors = observed[side]
            print(f"  {side.upper()} observed {len(behaviors)} behaviors")
            for behavior in sorted(behaviors, key=lambda b: (b["target_name"],
                                                             b["operation_name"])):
                print(f"    {behavior['operation_name']:<5} -> "
                      f"{behavior['target_name']:<22} "
                      f"{behavior['observations']} observation(s)")
        print("  BEHAVIORAL DIFF")
        print(f"    shared  {diff['shared_count']}")
        print(f"    removed {diff['removed_count']}")
        print(f"    added   {diff['added_count']}")
        for delta in diff["deltas"]:
            if delta["change"] == "added":
                behavior = delta["behavior"]
                print(f"      + {behavior.get('operation_name','')} "
                      f"-> {behavior.get('target_name','')}")
        print("  GATE")
        print(f"    verdict {gate['verdict'].upper()}")
        for name in ("added_behaviors", "block_decisions",
                     "critical_risk_observations"):
            check = gate[name]
            mark = "ok " if check["passed"] else "FAIL"
            print(f"    {mark} {name}: {check['actual']} / max {check['maximum']}")
        print(f"  compare exit code: {status}")

    def write_results(self, path):
        path = pathlib.Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "scenario": self.scenario.name,
            "runs": self.scenario.runs,
            "gate": self.scenario.gate,
            "comparisons": self.results,
        }, indent=2))
        return path


class _nothing:
    """A context manager for the case where there is nothing to manage."""

    def __enter__(self):
        return None

    def __exit__(self, *_):
        return False

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
        """One evaluation run, through the same wrapper `make demo` uses."""
        run_id = side.run_id(self.scenario.name, repetition)
        summary = self.root / ".runtime" / f"{run_id}-summary.json"

        command = [
            str(self.root / "scripts" / "tv-dev.sh"),
            "--api-url", api_url,
            "--run-id", run_id,
            "--candidate", side.candidate,
            "--behavioral-profile", f"{self.scenario.name}-{side.name}",
            "--instrumentation", self.scenario.instrumentation,
            "--summary-file", str(summary),
            "--wait-timeout", "180",
        ]
        if self.scenario.evidence == "records":
            command += ["--expect-records", str(side.records)]
        else:
            command += ["--expect-records-from", str(summary)]
        if self.stream:
            command += ["--stream"]
        command += ["--"] + [self._resolve(token) for token in side.command]

        environment = dict(**side.env)
        if mock_port is not None:
            environment["SUPPORT_AGENT_PORT"] = str(mock_port)
        environment["OLLAMA_MODEL"] = self.model

        import os
        child_env = dict(os.environ)
        child_env.update(environment)

        completed = subprocess.run(
            command, cwd=side.workdir or str(self.root), env=child_env)
        if completed.returncode != 0:
            raise world.WorldError(
                f"the {side.name} workload (repetition {repetition}) did not "
                f"produce evidence; run {run_id} was failed, not completed")
        return run_id

    def _resolve(self, token):
        """`python` in a scenario means the demo-managed interpreter.

        Spelled out rather than left to PATH: which Python runs decides which
        OpenTelemetry runtime is attached, and inheriting whatever happens to
        be first on PATH is how a scenario silently stops being instrumented.
        """
        if token == "python":
            return str(self.venv_python)
        return token

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
            candidate = self._invoke(scenario.candidate, repetition, port, api_url)

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

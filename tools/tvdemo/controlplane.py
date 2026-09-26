"""A thin adapter over the `trustvian` CLI.

Thin in the sense ADR 0033 means: it issues commands and reports what came
back. It computes no diff, no scorecard, no gate and no policy outcome, and
it never re-derives a verdict the server already gave.

Exit codes are the CLI's, and the distinction matters more than it looks:

    0   gate PASS
    1   gate FAIL          — only ever from `eval compare`
    2   usage
    3   operational — API, network, or the workload failing to run

A broken network reported as a policy violation would make every CI failure
ambiguous, so `OperationalError` is raised for anything that is not 0 or 1.
"""

from __future__ import annotations

import json
import subprocess


class OperationalError(Exception):
    """The control plane could not answer. Never a gate result."""


class ControlPlane:
    def __init__(self, binary, api_url):
        self.binary = str(binary)
        self.api_url = api_url

    def _run(self, args, allow=(0,)):
        completed = subprocess.run(
            [self.binary, *args, "--api-url", self.api_url],
            capture_output=True, text=True)
        if completed.returncode not in allow:
            raise OperationalError(
                f"trustvian {' '.join(args)} exited {completed.returncode}: "
                f"{completed.stderr.strip() or completed.stdout.strip()}")
        return completed

    def progress(self, run_id) -> dict:
        completed = self._run(["eval", "progress", "--id", run_id, "--json"])
        return json.loads(completed.stdout)

    def compare(self, reference_run, candidate_run, limits) -> tuple:
        """One server-side comparison.

        Returns (verdict_exit_code, payload). Exit 1 is a gate FAIL and a
        perfectly good result — `comparison.json` is written on both 0 and 1,
        because CI needs the evidence to publish alongside the failure.
        """
        args = [
            "eval", "compare",
            "--reference-run", reference_run,
            "--candidate-run", candidate_run,
            "--max-added-behaviors", str(limits["max_added_behaviors"]),
            "--max-block-decisions", str(limits["max_block_decisions"]),
            "--max-critical-risk-observations",
            str(limits["max_critical_risk_observations"]),
            "--json",
        ]
        completed = self._run(args, allow=(0, 1))
        return completed.returncode, json.loads(completed.stdout)

    def behaviors_of(self, run_id, limits) -> list:
        """Every behavior one run observed, read from a control-plane response.

        The `/v1` API exposes no per-run behavior snapshot: `progress` returns
        counts, and `POST /v1/evaluations/compare` is the only route that
        returns behaviors at all. So the run is compared against *itself* —
        which `CompareEvaluations` permits, the same-run refusal existing only
        for promotions — and every delta comes back `shared`. That delta list
        is the run's behavior set, computed by the server.

        This reimplements no diff. It reads one.
        """
        status, payload = self.compare(run_id, run_id, limits)
        deltas = payload.get("behavior_diff", {}).get("deltas", [])
        added = payload.get("behavior_diff", {}).get("added_count", 0)
        removed = payload.get("behavior_diff", {}).get("removed_count", 0)
        if added or removed:
            # Self-compare is undocumented behavior rather than a contract.
            # If a future build ever made it mean something else, say so here
            # instead of quietly reporting a wrong behavior set.
            raise OperationalError(
                f"comparing run {run_id} against itself reported {added} added "
                f"and {removed} removed behaviors, which cannot be true. This "
                f"reading of a run's behavior set is no longer valid against "
                f"this Trustvian build.")
        return [
            {
                "fingerprint_id": delta["fingerprint_id"],
                "operation_name": delta["behavior"].get("operation_name", ""),
                "operation_category": delta["behavior"].get("operation_category", ""),
                "target_name": delta["behavior"].get("target_name", ""),
                "observations": int(delta["reference_observations"]),
            }
            for delta in deltas
        ]

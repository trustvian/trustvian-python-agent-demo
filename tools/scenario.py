#!/usr/bin/env python3
"""Run one behavioral scenario.

    tools/scenario.py scenarios/<name>.yaml [--api-url URL] [--stream]

A stand-in for Trustvian task 078's scenario runner. It probes the built CLI
for the real one on every invocation and says so when it finds it.
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from tvdemo import controlplane, runner, scenario as scenario_mod, world

ROOT = pathlib.Path(__file__).resolve().parents[1]


def real_runner_available() -> bool:
    """Ask the built binary whether task 078 has shipped."""
    binary = ROOT / ".demo" / "bin" / "trustvian"
    if not binary.exists():
        return False
    completed = subprocess.run([str(binary), "eval", "--help"],
                               capture_output=True, text=True)
    return "scenario" in (completed.stdout + completed.stderr).lower()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", help="path to a scenario YAML file")
    parser.add_argument("--api-url", default=None,
                        help="attach to a control plane someone else started")
    parser.add_argument("--model", default="gemma3:4b")
    parser.add_argument("--stream", action="store_true",
                        help="also send the workload's output to the terminal")
    parser.add_argument("--results", default=None,
                        help="write the machine-readable result here")
    args = parser.parse_args(argv)

    try:
        spec = scenario_mod.load(args.scenario)
    except scenario_mod.ScenarioError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return runner.EXIT_USAGE

    if real_runner_available():
        print("note: this Trustvian build ships a scenario runner (task 078).")
        print("      tools/scenario.py is a stand-in and should now be retired.")

    print()
    print(f"scenario {spec.name}")
    if spec.description:
        print(f"  {spec.description}")
    print(f"  runs: {spec.runs} per side")
    print(f"  gate: " + ", ".join(f"{k} = {v}" for k, v in spec.gate.items()))

    engine = runner.Runner(spec, ROOT, api_url=args.api_url, model=args.model,
                           stream=args.stream)
    try:
        status = engine.run()
    except (world.WorldError, controlplane.OperationalError) as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        print("this is an operational failure, not a gate result.", file=sys.stderr)
        return runner.EXIT_OPERATIONAL

    if args.results:
        print(f"\nresults written to {engine.write_results(args.results)}")

    # Said plainly, because a bare exit 1 from a build tool reads as a broken
    # run. It is not: 1 is the gate's own verdict, and the code that means
    # "something went wrong" is 3.
    print()
    if status == runner.EXIT_GATE_FAIL:
        print("gate FAIL. Exit 1 is the gate's verdict under the limits this")
        print("scenario declared — not a broken run, and not a finding that the")
        print("candidate is unsafe, malicious or compromised. An API or network")
        print("failure would be exit 3.")
    else:
        print("gate PASS. Exit 0.")
    return status


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Measure how often an unchanged agent fails a single-run gate against itself.

    tools/stability.py scenarios/stability.yaml --runs 10 --temperature 0.7

The number Trustvian task 078's "Measurement before implementation" section asks
for. It is a measurement, so it is reported as measured: a run that does not
behave as expected is printed, not retried until it does.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from tvdemo import controlplane, runner, scenario as scenario_mod, stability, world

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", help="path to a scenario YAML file")
    parser.add_argument("--runs", type=int, default=10,
                        help="repetitions per configuration (default 10)")
    parser.add_argument("--temperature", default="0.7",
                        help="sampling temperature for the model (default 0.7)")
    parser.add_argument("--model", default="gemma3:4b")
    parser.add_argument("--api-url", default=None,
                        help="attach to a control plane someone else started")
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--results", default=None,
                        help="write the machine-readable report here")
    args = parser.parse_args(argv)

    if args.runs < 2:
        print("error: --runs must be at least 2; a sweep of one run has no pair "
              "to compare", file=sys.stderr)
        return runner.EXIT_USAGE

    try:
        spec = scenario_mod.load(args.scenario)
    except scenario_mod.ScenarioError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return runner.EXIT_USAGE

    print()
    print(f"stability measurement: {spec.name}")
    print(f"  {args.runs} repetitions of the UNCHANGED reference side, in each of")
    print(f"  two configurations, compared against itself.")
    if spec.needs_a_model:
        print(f"  model {args.model} at temperature {args.temperature}")
    else:
        print(f"  SIMULATION — no model. These numbers exercise the measurement")
        print(f"  path and are never published as evidence about an agent.")
    print()

    try:
        if spec.needs_a_model:
            world.require_ollama(args.model)

        with world.Runtime(ROOT, args.api_url) as runtime:
            plane = controlplane.ControlPlane(
                ROOT / ".demo" / "bin" / "trustvian", runtime.api_url)
            mocks = None
            if "mocks" in spec.services:
                mocks = world.Mocks(ROOT, ROOT / ".demo" / "venv" / "bin" / "python")

            context = mocks if mocks is not None else _nothing()
            with context:
                sweep = stability.Sweep(
                    spec, ROOT, plane, runtime.api_url,
                    runs=args.runs, model=args.model,
                    temperature=args.temperature,
                    mock_port=mocks.port if mocks is not None else None,
                    stream=args.stream)
                report = sweep.run()
    except (world.WorldError, controlplane.OperationalError) as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        print("this is an operational failure, not a measurement.", file=sys.stderr)
        return runner.EXIT_OPERATIONAL

    print()
    print(stability.render(report))

    if args.results:
        written = stability.write(report, args.results)
        print()
        print(f"machine-readable report: {written}")

    # Always 0. This command measures; it does not gate. A sweep that exited 1
    # because the thing it was measuring occurred would be unusable for the one
    # job it has.
    return runner.EXIT_PASS


class _nothing:
    def __enter__(self):
        return None

    def __exit__(self, *_):
        return False


if __name__ == "__main__":
    sys.exit(main())

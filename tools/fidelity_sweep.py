#!/usr/bin/env python3
"""Re-run Trustvian task 078's measurement at tool-name fidelity.

    tools/fidelity_sweep.py scenarios/tool-fidelity-sweep.yaml \\
        --runs 10 --temperature 0.7 --timeout 1200 \\
        --results docs/results/<date>-stability-tool-fidelity.json

N repetitions of the reference code and N of the candidate code, alternating
and sequential, each under its own fresh behavioral profile. Every attempt is
recorded with its outcome. Every verdict is the control plane's; the analysis
is offline and labelled so. See tools/tvdemo/fidelity_sweep.py.

Exit codes: 0 the sweep ran and its results were written (whatever they say),
2 usage, 3 the sweep could not run or could not be read back.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import platform as host_platform
import subprocess
import sys
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from tvdemo import controlplane, runner, scenario as scenario_mod, world  # noqa: E402
from tvdemo import fidelity_sweep as fs  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _git(*args):
    completed = subprocess.run(["git", "-C", str(ROOT), *args],
                               capture_output=True, text=True)
    return completed.stdout.strip() if completed.returncode == 0 else ""


def provenance(model, api_url):
    """What produced these numbers, recorded beside them."""
    version = subprocess.run([str(ROOT / ".demo" / "bin" / "trustvian"), "version"],
                             capture_output=True, text=True).stdout
    revision = ""
    for line in version.splitlines():
        if line.strip().startswith("revision:"):
            revision = line.split(":", 1)[1].strip()
    stamp = (ROOT / ".demo" / "bin" / ".build-stamp")
    model_digest, ollama_version = "", ""
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=5) as r:
            for m in json.loads(r.read()).get("models", []):
                if m.get("name") == model:
                    model_digest = m.get("digest", "")
                    details = m.get("details", {})
                    model_details = {k: details.get(k, "") for k in
                                     ("family", "parameter_size", "quantization_level")}
        with urllib.request.urlopen("http://127.0.0.1:11434/api/version", timeout=5) as r:
            ollama_version = json.loads(r.read()).get("version", "")
    except (OSError, ValueError):
        model_details = {}
    return {
        "trustvian_revision": revision,
        "trustvian_build_stamp": stamp.read_text().strip() if stamp.exists() else "",
        "demo_commit": _git("rev-parse", "HEAD"),
        "demo_dirty": bool(_git("status", "--porcelain")),
        "model": model,
        "model_digest": model_digest,
        "model_details": model_details,
        "ollama_version": ollama_version,
        "host": {"system": host_platform.system(), "machine": host_platform.machine(),
                 "python": host_platform.python_version()},
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenario")
    parser.add_argument("--runs", type=int, required=True,
                        help="repetitions per side; required, no default")
    parser.add_argument("--temperature", required=True)
    parser.add_argument("--timeout", type=int, required=True,
                        help="seconds per repetition before it is killed and recorded")
    parser.add_argument("--model", default="gemma3:4b")
    parser.add_argument("--max-consecutive-failures", type=int, default=3)
    parser.add_argument("--results", required=True)
    parser.add_argument("--namespace", default=None)
    args = parser.parse_args(argv)

    if not 2 <= args.runs <= 64:
        print("error: --runs must be within 2..64", file=sys.stderr)
        return runner.EXIT_USAGE
    if args.timeout < 60:
        print("error: --timeout below 60s cannot run one model-driven repetition",
              file=sys.stderr)
        return runner.EXIT_USAGE
    try:
        spec = scenario_mod.load(args.scenario)
    except scenario_mod.ScenarioError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return runner.EXIT_USAGE

    results = pathlib.Path(args.results)
    checkpoint = results.with_suffix(".partial.json")

    try:
        if spec.needs_a_model:
            world.require_ollama(args.model)
        with world.Runtime(ROOT) as runtime:
            plane = controlplane.ControlPlane(ROOT / ".demo" / "bin" / "trustvian",
                                              runtime.api_url)
            api = fs.Api(runtime.api_url)
            mocks = world.Mocks(ROOT, ROOT / ".demo" / "venv" / "bin" / "python") \
                if "mocks" in spec.services else None
            with (mocks if mocks is not None else _Nothing()):
                sweep = fs.Sweep(
                    spec, ROOT, plane, api, runs=args.runs, model=args.model,
                    temperature=args.temperature, timeout=args.timeout,
                    mock_port=mocks.port if mocks else None,
                    log_dir=ROOT / ".demo" / "fidelity-logs",
                    namespace=args.namespace,
                    max_consecutive_failures=args.max_consecutive_failures)
                document = {
                    "schema_version": fs.RESULTS_SCHEMA_VERSION,
                    "method": {
                        "scenario": spec.name,
                        "simulation": not spec.needs_a_model,
                        "runs_per_side": args.runs,
                        "temperature": args.temperature,
                        "timeout_seconds": args.timeout,
                        "max_consecutive_failures": args.max_consecutive_failures,
                        "schedule": "sequential, alternating reference/candidate",
                        "learning": "one fresh --behavioral-profile per repetition",
                        "gate_limits": dict(spec.gate),
                        "workload": {side: {"command": list(getattr(spec, side).command),
                                            "env": dict(getattr(spec, side).env)}
                                     for side in fs.SIDES},
                    },
                    "provenance": provenance(args.model, runtime.api_url),
                    "namespace": sweep.namespace,
                }
                print(f"sweep {sweep.namespace}: {args.runs} per side, "
                      f"T={args.temperature}, timeout {args.timeout}s", flush=True)

                attempts_so_far = []

                def on_attempt(record):
                    attempts_so_far.append(record)
                    print(f"  {record['side']:<9} {record['repetition']:>2}  "
                          f"{record['outcome']:<20} "
                          f"{record.get('seconds', '-')}s  "
                          f"records={record.get('record_count', '-')}", flush=True)
                    fs.write({**document, "attempts": attempts_so_far,
                              "partial": True}, checkpoint)

                result = sweep.run(on_attempt=on_attempt)
                comparisons = fs.compare_all(plane, result["attempts"], spec.gate)
                document.update(
                    attempts=result["attempts"], aborted=result["aborted"],
                    comparisons=comparisons,
                    analysis=fs.analyze(result, comparisons, args.runs))
                fs.write(document, results)
                if checkpoint.exists():
                    checkpoint.unlink()
    except (world.WorldError, controlplane.OperationalError, fs.SweepError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return runner.EXIT_OPERATIONAL

    print(f"wrote {results}")
    return 0


class _Nothing:
    def __enter__(self):
        return None

    def __exit__(self, *_):
        return False


if __name__ == "__main__":
    sys.exit(main())

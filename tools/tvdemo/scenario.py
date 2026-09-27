"""The scenario file: what to run, how many times, and what it must satisfy.

Shaped after Trustvian task 078's illustrative YAML, plus the `runs` field
that task's objective now calls for. This is a stand-in for the real scenario
format, and it will be replaced by it — `scenario_runner_available` in
scripts/lib.sh probes the built CLI for the real one on every invocation.

A scenario file is executable configuration: it names a command, and that
command is run from the developer's own repository under their own
privileges, exactly as a Makefile or a test script is. There is no sandbox
here and the format does not imply one.
"""

from __future__ import annotations

import dataclasses
import pathlib

import yaml

SCHEMA_VERSION = "1"

# Task 056's three maximums. Every one must be stated: zero is the strictest
# limit there is, so defaulting an omitted limit to zero would fail a build
# under a policy nobody chose, and it would look exactly like a real
# regression.
GATE_LIMITS = (
    "max_added_behaviors",
    "max_block_decisions",
    "max_critical_risk_observations",
)

EVIDENCE_SOURCES = ("summary", "records")

# `trustvian dev`'s own modes, and only those. The mode is passed through to it
# verbatim, so a value it does not know would be a usage error from dev in the
# middle of a scenario rather than here, before anything runs.
#
# dev's fourth mode, python-zero-code, is reserved and refused in this build —
# the workloads here name opentelemetry-instrument in their own command instead,
# which is what `existing` then truthfully describes.
INSTRUMENTATION_MODES = ("existing", "none", "auto")

# How the N repetitions of one side relate to each other's learned baseline.
#
# `shared` is what a developer gets by default: one candidate id, so one
# learning scope, so repetition i is analyzed against a baseline that learned
# from 1..i-1. `isolated` allocates a candidate per repetition, which is what
# Trustvian task 078 specifies for scenario suites — its presence counts must
# measure the workload's nondeterminism, not the order the repetitions ran in.
#
# Both are real configurations answering different questions, so neither is
# hidden behind the other.
LEARNING_MODES = ("shared", "isolated")


class ScenarioError(Exception):
    """The scenario file cannot be used.

    Always a usage failure — exit 2, before anything runs. A malformed
    scenario is not an operational problem and is certainly not a gate
    result.
    """


@dataclasses.dataclass(frozen=True)
class Side:
    """One half of the comparison: the reference, or the candidate."""

    name: str
    candidate: str
    env: dict
    command: list
    workdir: str | None
    records: int | None
    learning: str

    def run_id(self, scenario: str, repetition: int) -> str:
        return f"{scenario}-{self.name}-{repetition}"

    def candidate_for(self, repetition: int) -> str:
        """The candidate id this repetition runs under.

        The candidate *is* the learning scope, so this is the only lever that
        decides whether the repetitions learn from each other. Under `isolated`
        each gets its own; under `shared` they all share one.

        The repetition index appears in an identifier here and nowhere else.
        Task 078 is explicit that the index is correlation metadata and never
        behavioral identity — a profile ref is an opaque string the engine never
        parses, so encoding an index in one is a runner convenience, not a
        contract.
        """
        if self.learning == "isolated":
            return f"{self.candidate}-rep-{repetition}"
        return self.candidate


@dataclasses.dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    runs: int
    evidence: str
    instrumentation: str
    learning: str
    services: tuple
    requires: tuple
    gate: dict
    reference: Side
    candidate: Side
    path: pathlib.Path

    @property
    def needs_a_model(self) -> bool:
        return "ollama" in self.requires


def _require(document: dict, key: str, where: str):
    if key not in document:
        raise ScenarioError(f"{where}: {key!r} is required")
    return document[key]


def _side(document: dict, name: str, shared_command: list, evidence: str,
          learning: str, where: str) -> Side:
    raw = _require(document, name, where)
    if not isinstance(raw, dict):
        raise ScenarioError(f"{where}: {name!r} must be a mapping")

    command = raw.get("command", shared_command)
    if not command:
        raise ScenarioError(
            f"{where}: {name!r} has no command, and no shared `command` is set")
    if not isinstance(command, list) or not all(isinstance(c, str) for c in command):
        raise ScenarioError(f"{where}: {name}.command must be a list of strings")

    env = raw.get("env", {})
    if not isinstance(env, dict):
        raise ScenarioError(f"{where}: {name}.env must be a mapping")

    records = None
    if evidence == "records":
        records = raw.get("records")
        if not isinstance(records, int) or records < 1:
            raise ScenarioError(
                f"{where}: evidence is 'records', so {name}.records must be a "
                f"positive integer saying how many to wait for")
    elif "records" in raw:
        raise ScenarioError(
            f"{where}: {name}.records is set, but evidence is {evidence!r}. "
            f"A workload whose activity is read from its own summary must not "
            f"also have a predicted count — one of the two would be wrong and "
            f"nothing would say which.")

    return Side(
        name=name,
        candidate=str(_require(raw, "candidate", f"{where}.{name}")),
        env={str(k): str(v) for k, v in env.items()},
        command=[str(c) for c in command],
        workdir=raw.get("workdir"),
        records=records,
        learning=learning,
    )


def load(path) -> Scenario:
    """Read and validate one scenario file.

    Every failure here is a ScenarioError, and every caller turns that into
    exit 2 before starting anything. Task 078 is explicit that a malformed
    scenario is a usage failure, reported before the workload runs.
    """
    path = pathlib.Path(path)
    where = str(path)
    try:
        document = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        raise ScenarioError(f"{where}: no such scenario file") from None
    except yaml.YAMLError as exc:
        raise ScenarioError(f"{where}: not valid YAML: {exc}") from None

    if not isinstance(document, dict):
        raise ScenarioError(f"{where}: a scenario must be a mapping")

    version = str(_require(document, "version", where))
    if version != SCHEMA_VERSION:
        raise ScenarioError(
            f"{where}: schema version {version!r} is not supported "
            f"(this tool reads {SCHEMA_VERSION!r})")

    runs = document.get("runs", 1)
    if not isinstance(runs, int) or runs < 1:
        raise ScenarioError(f"{where}: `runs` must be a positive integer")

    evidence = str(document.get("evidence", "summary"))
    if evidence not in EVIDENCE_SOURCES:
        raise ScenarioError(
            f"{where}: `evidence` must be one of {', '.join(EVIDENCE_SOURCES)}")

    instrumentation = str(document.get("instrumentation", "existing"))
    if instrumentation not in INSTRUMENTATION_MODES:
        raise ScenarioError(
            f"{where}: `instrumentation` must be one of "
            f"{', '.join(INSTRUMENTATION_MODES)} — these are trustvian dev's own "
            f"modes, and the value is passed to it unchanged")

    learning = str(document.get("learning", "shared"))
    if learning not in LEARNING_MODES:
        raise ScenarioError(
            f"{where}: `learning` must be one of {', '.join(LEARNING_MODES)}")

    gate = _require(document, "gate", where)
    if not isinstance(gate, dict):
        raise ScenarioError(f"{where}: `gate` must be a mapping")
    limits = {}
    for limit in GATE_LIMITS:
        if limit not in gate:
            raise ScenarioError(
                f"{where}: gate limit {limit!r} is required. Zero is a strict "
                f"limit, not a default — an omitted one would apply the "
                f"strictest possible policy to someone who simply forgot it.")
        value = gate[limit]
        if not isinstance(value, int) or value < 0:
            raise ScenarioError(f"{where}: gate.{limit} must be a non-negative integer")
        limits[limit] = value
    unknown = set(gate) - set(GATE_LIMITS)
    if unknown:
        raise ScenarioError(
            f"{where}: unknown gate limit(s) {', '.join(sorted(unknown))}")

    shared_command = document.get("command", [])
    services = tuple(document.get("services", []) or [])
    requires = tuple(document.get("requires", []) or [])

    return Scenario(
        name=str(_require(document, "name", where)),
        description=str(document.get("description", "")),
        runs=runs,
        evidence=evidence,
        instrumentation=instrumentation,
        learning=learning,
        services=services,
        requires=requires,
        gate=limits,
        reference=_side(document, "reference", shared_command, evidence, learning, where),
        candidate=_side(document, "candidate", shared_command, evidence, learning, where),
        path=path,
    )

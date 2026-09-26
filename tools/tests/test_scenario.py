"""The scenario file's contract.

Every failure here must be a usage failure reported before anything runs.
Task 078 is explicit about that, and about the one rule that is easiest to
get wrong: every gate limit is required, because zero is the strictest limit
there is and defaulting to it would fail a build under a policy nobody chose.

These tests read files and parse text. They start no server and run no model.

They run from `.demo/tools-venv`, the tooling's own interpreter, because the
loader depends on PyYAML and the agent's interpreter deliberately does not
have it.
"""

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from tvdemo import scenario as scenario_mod

SCENARIOS = sorted((ROOT / "scenarios").rglob("*.yaml"))

MINIMAL = """
version: "1"
name: t
command: [python, x.py]
evidence: records
reference: {candidate: reference, records: 1}
candidate: {candidate: candidate, records: 1}
gate:
  max_added_behaviors: 0
  max_block_decisions: 0
  max_critical_risk_observations: 0
"""


class ScenarioValidationTest(unittest.TestCase):
    def load(self, text):
        path = pathlib.Path(self.enterContext(
            __import__("tempfile").TemporaryDirectory())) / "s.yaml"
        path.write_text(text)
        return scenario_mod.load(path)

    def test_a_minimal_scenario_loads(self):
        spec = self.load(MINIMAL)
        self.assertEqual(spec.name, "t")
        self.assertEqual(spec.runs, 1)
        self.assertEqual(spec.reference.candidate, "reference")

    def test_every_gate_limit_is_required(self):
        for omitted in scenario_mod.GATE_LIMITS:
            with self.subTest(omitted=omitted):
                text = "\n".join(line for line in MINIMAL.splitlines()
                                 if omitted not in line)
                with self.assertRaises(scenario_mod.ScenarioError) as caught:
                    self.load(text)
                # The message must name the limit, so the fix is obvious.
                self.assertIn(omitted, str(caught.exception))

    def test_an_unknown_gate_limit_is_refused(self):
        text = MINIMAL + "  max_everything_else: 4\n"
        with self.assertRaises(scenario_mod.ScenarioError):
            self.load(text)

    def test_runs_must_be_a_positive_integer(self):
        for bad in ("0", "-1", "'many'"):
            with self.subTest(runs=bad):
                with self.assertRaises(scenario_mod.ScenarioError):
                    self.load(MINIMAL + f"runs: {bad}\n")

    def test_an_unsupported_schema_version_is_refused(self):
        with self.assertRaises(scenario_mod.ScenarioError):
            self.load(MINIMAL.replace('version: "1"', 'version: "2"'))

    def test_a_predicted_count_and_a_reported_one_cannot_both_be_given(self):
        # One of the two would be wrong and nothing would say which.
        text = MINIMAL.replace("evidence: records", "evidence: summary")
        with self.assertRaises(scenario_mod.ScenarioError):
            self.load(text)

    def test_malformed_yaml_is_a_usage_error_not_a_crash(self):
        with self.assertRaises(scenario_mod.ScenarioError):
            self.load("version: \"1\"\nname: [unclosed\n")

    def test_a_missing_file_is_a_usage_error(self):
        with self.assertRaises(scenario_mod.ScenarioError):
            scenario_mod.load(ROOT / "scenarios" / "does-not-exist.yaml")


class CommittedScenarioTest(unittest.TestCase):
    """Every scenario in the repository is loadable and states its limits."""

    def test_there_is_at_least_one(self):
        self.assertTrue(SCENARIOS, "no scenarios/*.yaml found")

    def test_each_one_loads(self):
        for path in SCENARIOS:
            with self.subTest(scenario=path.name):
                spec = scenario_mod.load(path)
                self.assertEqual(sorted(spec.gate), sorted(scenario_mod.GATE_LIMITS))

    def test_a_model_free_scenario_exists(self):
        # CI's default jobs must never need Ollama or a model.
        modelless = [scenario_mod.load(p) for p in SCENARIOS]
        self.assertTrue(any(not s.needs_a_model for s in modelless),
                        "no scenario runs without a model")


if __name__ == "__main__":
    unittest.main()

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



class LearningModeTest(ScenarioValidationTest):
    """`learning` decides whether the N repetitions teach each other."""

    def test_shared_is_the_default(self):
        spec = self.load(MINIMAL)
        self.assertEqual(spec.learning, "shared")
        # One candidate id for every repetition, so one learning scope.
        self.assertEqual(spec.reference.candidate_for(1),
                         spec.reference.candidate_for(2))

    def test_isolated_gives_each_repetition_its_own_candidate(self):
        spec = self.load(MINIMAL + "learning: isolated\n")
        first = spec.reference.candidate_for(1)
        second = spec.reference.candidate_for(2)
        self.assertNotEqual(first, second)
        self.assertTrue(first.endswith("-rep-1"), first)

    def test_an_unknown_learning_mode_is_a_usage_error(self):
        with self.assertRaises(scenario_mod.ScenarioError) as caught:
            self.load(MINIMAL + "learning: sometimes\n")
        self.assertIn("learning", str(caught.exception))


class InstrumentationModeTest(ScenarioValidationTest):
    """The mode reaches `trustvian dev` unchanged, so it is dev's vocabulary."""

    def test_existing_is_the_default(self):
        self.assertEqual(self.load(MINIMAL).instrumentation, "existing")

    def test_devs_own_modes_are_accepted(self):
        for mode in ("existing", "none", "auto"):
            with self.subTest(mode=mode):
                spec = self.load(MINIMAL + f"instrumentation: {mode}\n")
                self.assertEqual(spec.instrumentation, mode)

    def test_a_mode_dev_does_not_know_is_refused_before_anything_runs(self):
        # python-zero-code is reserved and refused by dev in this build. A
        # scenario naming it would fail from dev in the middle of a run, which is
        # later and less useful than failing here.
        for mode in ("python-zero-code", "inject"):
            with self.subTest(mode=mode):
                with self.assertRaises(scenario_mod.ScenarioError):
                    self.load(MINIMAL + f"instrumentation: {mode}\n")


class ThresholdTest(unittest.TestCase):
    """The k a sweep reports, computed from presence counts alone."""

    def counts(self, *runs_present):
        return [{"runs_present": n} for n in runs_present]

    def test_k_is_one_above_the_noisiest_varying_behavior(self):
        from tvdemo import stability
        # Two behaviors always present, one in three of five runs: a threshold of
        # four would have reported nothing.
        self.assertEqual(
            stability.zero_false_fail_threshold(self.counts(5, 5, 3), 5), 4)

    def test_no_threshold_is_reported_when_nothing_varied(self):
        from tvdemo import stability
        # Every behavior in every run means the single-run gate had nothing to
        # absorb, and task 078 says that outcome must be reportable rather than
        # rounded into a recommendation.
        self.assertIsNone(
            stability.zero_false_fail_threshold(self.counts(5, 5, 5), 5))

    def test_a_behavior_seen_once_still_needs_a_threshold_above_it(self):
        from tvdemo import stability
        self.assertEqual(
            stability.zero_false_fail_threshold(self.counts(10, 1), 10), 2)


class SimulationLabellingTest(unittest.TestCase):
    """The seeded fixture must be unmistakable for a model.

    A seeded RNG read as evidence about a language model is the one way this
    repository's stability numbers become dishonest, so the label is asserted
    rather than trusted to reviewers.
    """

    def test_the_fixture_calls_itself_a_simulation(self):
        source = (ROOT / "fixtures" / "stochastic_agent.py").read_text()
        self.assertIn("SIMULATION", source)
        # And at runtime, not only in a docstring nobody reads.
        self.assertIn('print(f"  SIMULATION', source)

    def test_the_simulation_scenario_says_so_too(self):
        text = (ROOT / "scenarios" / "stability-simulation.yaml").read_text()
        self.assertIn("SIMULATION", text)
        self.assertIn("NOT an agent", text)

    def test_the_simulation_requires_a_seed_with_no_default(self):
        # An unseeded simulation is not reproducible, and a default seed would
        # make every repetition identical and silently measure nothing.
        source = (ROOT / "fixtures" / "stochastic_agent.py").read_text()
        self.assertIn("SUPPORT_AGENT_SEED", source)
        self.assertNotIn('os.environ.get("SUPPORT_AGENT_SEED", ', source)

    def test_the_real_measurement_scenario_needs_a_model(self):
        spec = scenario_mod.load(ROOT / "scenarios" / "stability.yaml")
        self.assertTrue(spec.needs_a_model)
        simulated = scenario_mod.load(
            ROOT / "scenarios" / "stability-simulation.yaml")
        self.assertFalse(simulated.needs_a_model)

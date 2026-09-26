"""The scenario runner is an adapter, and must not grow an opinion.

Asserted by reading the sources, in the same style that keeps the Trustvian
CLI from reimplementing the gate. A runner that computed its own diff would
duplicate what ADR 0023 and ADR 0033 exist to keep in one place, and it would
drift from the server's answer the first time either changed.

This file reads text and imports nothing, which is why it lives with the
agent's tests rather than the tooling's: it must run everywhere.
"""

import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class RunnerOwnsNoVerdictTest(unittest.TestCase):
    """The runner is an adapter. It must not grow an opinion.

    Asserted by reading the sources, in the same style that keeps the
    Trustvian CLI from reimplementing the gate. A runner that computed its own
    diff would drift from the server's answer the first time either changed.
    """

    RUNNER_SOURCES = ("runner.py", "controlplane.py", "scenario.py", "world.py")

    def sources(self):
        for name in self.RUNNER_SOURCES:
            yield name, (ROOT / "tools" / "tvdemo" / name).read_text()

    def test_no_ordering_comparator_exists(self):
        # Task 078: a scenario asserts no action ordering of its own. The
        # engine's sequence evidence stays authoritative and reaches the
        # verdict through the scorecard, not through anything here.
        for name, source in self.sources():
            with self.subTest(source=name):
                for forbidden in ("expected_sequence", "expected_steps",
                                  "step_order", "in_order"):
                    self.assertNotIn(forbidden, source)

    def test_the_verdict_is_never_derived_locally(self):
        for name, source in self.sources():
            with self.subTest(source=name):
                # "fail"/"pass" may be *read* from a response; they may never
                # be assigned as a locally computed verdict.
                for forbidden in ('verdict = "fail"', "verdict = 'fail'",
                                  'verdict = "pass"', "verdict = 'pass'"):
                    self.assertNotIn(forbidden, source)

    def test_added_behaviors_are_never_counted_here(self):
        # added_count is the server's. Reading it is fine; recomputing it by
        # differencing two behavior sets is not.
        source = (ROOT / "tools" / "tvdemo" / "runner.py").read_text()
        self.assertNotIn("set(", source)
        self.assertNotIn("difference(", source)


if __name__ == "__main__":
    unittest.main()

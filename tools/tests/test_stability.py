"""The stability sweep's own arithmetic.

Everything in `tvdemo.stability` that turns control-plane responses into a
published number, tested without a control plane and without a model. The
responses are the platform's; the grouping and the counting are this
repository's, and that is exactly the part that has to be right — a sweep whose
aggregation is wrong reports a plausible figure about nothing.

These tests start no server and run no model. They run from
`.demo/tools-venv`, the tooling's own interpreter.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tvdemo import stability

class FalseFailRateByThresholdTest(unittest.TestCase):
    """What a k-of-N gate would have reported over groups of runs.

    Every run in a sweep is the same unchanged agent, so any behavior the rule
    calls repeatedly added is a false FAIL by construction — which is what makes
    the rate measurable without a labelled positive.
    """

    @staticmethod
    def _runs(total, rare_present):
        return [{"behaviors": [{"fingerprint_id": "always"}] +
                 ([{"fingerprint_id": "rare"}] if i < rare_present else [])}
                for i in range(total)]

    def rate(self, total, rare_present, k, group_size=5):
        rows = stability.false_fail_rate_by_threshold(
            self._runs(total, rare_present), group_size, [k])
        return rows[0]["false_fail_rate"]

    def test_a_behavior_in_every_run_never_produces_a_finding(self):
        """The property that makes set semantics safe on a stable workload."""
        for k in (1, 3, 5):
            with self.subTest(k=k):
                self.assertEqual(self.rate(10, 10, k), 0.0)

    def test_a_rarer_behavior_produces_a_higher_rate_at_k_equals_one(self):
        """Monotonic in rarity, which is the phenomenon k-of-N absorbs."""
        rates = [self.rate(10, present, 1) for present in (2, 3, 4, 5)]
        self.assertEqual(rates, sorted(rates, reverse=True),
                         f"not decreasing in presence: {rates}")

    def test_raising_k_never_raises_the_rate(self):
        """A higher bar cannot admit more findings."""
        rates = [self.rate(10, 3, k) for k in (1, 2, 3, 4, 5)]
        for earlier, later in zip(rates, rates[1:]):
            self.assertLessEqual(later, earlier, f"k raised the rate: {rates}")

    def test_k_above_the_presence_count_is_unreachable(self):
        """A behavior in 2 runs cannot be present in 3 of a candidate group."""
        self.assertEqual(self.rate(10, 2, 3), 0.0)
        self.assertGreater(self.rate(10, 2, 2), 0.0)

    def test_the_split_count_is_every_disjoint_pair_of_groups(self):
        """C(10,5) reference choices, each with exactly one complement."""
        rows = stability.false_fail_rate_by_threshold(self._runs(10, 10), 5, [1])
        self.assertEqual(rows[0]["splits"], 252)

    def test_a_group_larger_than_half_the_runs_is_refused(self):
        """Rather than silently overlapping the two sides, which would compare a
        run against itself and make every behavior look shared."""
        with self.assertRaises(ValueError) as caught:
            stability.false_fail_rate_by_threshold(self._runs(10, 10), 6, [1])
        self.assertIn("needs 12", str(caught.exception))

    def test_j_admits_a_behavior_the_reference_showed_rarely(self):
        """j > 0 is what a caller raises when the reference side also varies."""
        runs = self._runs(10, 4)
        strict = stability.false_fail_rate_by_threshold(runs, 5, [1], j=0)[0]
        lenient = stability.false_fail_rate_by_threshold(runs, 5, [1], j=1)[0]
        self.assertGreater(lenient["false_fail_rate"], strict["false_fail_rate"],
                           "raising j should admit more splits as 'added'")

"""The tool-fidelity sweep's harness: isolation, accounting, bounds, cleanup,
output integrity — and the offline analysis it publishes.

No model and no control plane: the executor, the /v1 reader and the comparison
adapter are replaced by fakes, and the one test that needs a real process runs a
short-lived `sh`. Run from `.demo/tools-venv`.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tvdemo import fidelity_sweep as fs
from tvdemo import scenario as scenario_mod

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCENARIO = ROOT / "scenarios" / "tool-fidelity-sweep.yaml"


def behavior(fp, category="tool", name=None, target=""):
    return {"fingerprint_id": fp, "observations": 1, "operation_category": category,
            "operation_name": name or fp, "target_name": target, "target_category": ""}


class FakeApi:
    """Serves a canned evidence record per run id; records what was asked."""

    api_url = "http://127.0.0.1:1"

    def __init__(self, evidence):
        self.evidence = evidence  # run_id -> dict, or an Exception to raise

    def _for(self, run_id):
        value = self.evidence.get(run_id)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise fs.SweepError(f"no such run {run_id}")
        return value

    def run(self, run_id):
        return {"status": self._for(run_id).get("status", "completed")}

    def progress(self, run_id):
        e = self._for(run_id)
        return {"record_count": str(e.get("records", 1)),
                "distinct_behavior_count": len(e.get("behaviors", [])),
                "behavior_complete": e.get("behavior_complete", True)}

    def behaviors(self, run_id):
        e = self._for(run_id)
        return e.get("complete", True), list(e.get("behaviors", []))

    def observation_order(self, run_id):
        e = self._for(run_id)
        return e.get("history", "complete"), list(e.get("order", []))


class Recorder:
    """An executor that records each call and returns a scripted result."""

    def __init__(self, results=None, on_call=None):
        self.calls = []
        self.results = results or {}
        self.on_call = on_call

    def __call__(self, command, env, cwd, timeout, log_path):
        run_id = command[command.index("--run-id") + 1]
        self.calls.append({"command": command, "env": env, "timeout": timeout,
                           "run_id": run_id})
        if self.on_call:
            self.on_call(command)
        rc, timed_out = self.results.get(run_id, (0, False))
        return {"returncode": rc, "timed_out": timed_out, "seconds": 1.0,
                "stragglers": False}


def make_sweep(api, executor, runs=2, state_root=None, **kw):
    spec = scenario_mod.load(SCENARIO)
    return fs.Sweep(spec, ROOT, plane=None, api=api, runs=runs, model="m",
                    temperature="0.7", timeout=900, namespace="t",
                    state_root=state_root or tempfile.mkdtemp(),
                    executor=executor, **kw)


def healthy(behaviors, order=None):
    return {"records": 5, "behaviors": behaviors,
            "order": order or [b["fingerprint_id"] for b in behaviors]}


class IsolationTest(unittest.TestCase):
    def test_every_repetition_gets_its_own_profile_and_one_candidate_per_side(self):
        sweep = make_sweep(FakeApi({}), Recorder(), runs=3)
        profiles = {sweep.profile(side, i) for side, i in sweep.schedule()}
        self.assertEqual(len(profiles), 6)
        self.assertEqual({sweep.candidate_id(fs.REFERENCE)}, {
            sweep.candidate_id(side) for side, _ in sweep.schedule() if side == fs.REFERENCE})
        self.assertNotEqual(sweep.candidate_id(fs.REFERENCE), sweep.candidate_id(fs.CANDIDATE))

    def test_the_profile_reaches_dev_as_behavioral_profile_not_as_a_candidate(self):
        rec = Recorder()
        sweep = make_sweep(FakeApi({"fid-t-reference-1": healthy([behavior("a")])}), rec, runs=1)
        sweep.attempt(fs.REFERENCE, 1)
        command = rec.calls[0]["command"]
        self.assertEqual(command[command.index("--behavioral-profile") + 1],
                         "fid-t-reference-p1")
        self.assertEqual(command[command.index("--candidate") + 1], "fid-t-reference")

    def test_a_profile_with_an_existing_baseline_is_refused_and_not_run(self):
        with tempfile.TemporaryDirectory() as state:
            os.makedirs(os.path.join(state, "workload-key"))
            pathlib.Path(state, "workload-key", "baseline-fid-t-reference-p1.json").write_text("{}")
            rec = Recorder()
            sweep = make_sweep(FakeApi({}), rec, runs=1, state_root=state)
            record = sweep.attempt(fs.REFERENCE, 1)
        self.assertEqual(record["outcome"], fs.ISOLATION_VIOLATION)
        self.assertEqual(rec.calls, [], "a contaminated profile must never run")

    def test_baseline_file_naming_matches_dev(self):
        # cmd/trustvian/dev_baseline.go: [A-Za-z0-9-.] kept, anything else _%02x.
        self.assertEqual(fs.baseline_segment("fid-x.p1"), "fid-x.p1")
        self.assertEqual(fs.baseline_segment("git:abc+dirty"), "git_3aabc_2bdirty")

    def test_schedule_is_sequential_and_alternating(self):
        sweep = make_sweep(FakeApi({}), Recorder(), runs=3)
        self.assertEqual(sweep.schedule(), [
            ("reference", 1), ("candidate", 1), ("reference", 2),
            ("candidate", 2), ("reference", 3), ("candidate", 3)])


class FailureAccountingTest(unittest.TestCase):
    def test_every_outcome_is_recorded_and_none_is_an_empty_success(self):
        evidence = {
            "fid-t-reference-1": healthy([behavior("a")]),
            "fid-t-candidate-1": {"records": 0, "behaviors": []},
            "fid-t-reference-2": {**healthy([behavior("a")]), "history": "partial"},
            "fid-t-candidate-2": healthy([behavior("a")]),
        }
        rec = Recorder(results={"fid-t-candidate-2": (1, False)})
        result = make_sweep(FakeApi(evidence), rec).run()
        outcomes = {(a["side"], a["repetition"]): a["outcome"] for a in result["attempts"]}
        self.assertEqual(outcomes, {
            ("reference", 1): fs.COMPLETED,
            ("candidate", 1): fs.EMPTY_EVIDENCE,
            ("reference", 2): fs.INCOMPLETE_EVIDENCE,
            ("candidate", 2): fs.WORKLOAD_FAILED,
        })
        self.assertEqual(len(result["attempts"]), 4, "every attempt is reported")

    def test_a_timeout_is_its_own_outcome(self):
        rec = Recorder(results={"fid-t-reference-1": (-15, True)})
        record = make_sweep(FakeApi({"fid-t-reference-1": healthy([behavior("a")])}),
                            rec, runs=1).attempt(fs.REFERENCE, 1)
        self.assertEqual(record["outcome"], fs.TIMED_OUT)

    def test_a_run_the_control_plane_cannot_return_is_not_evidence(self):
        record = make_sweep(FakeApi({"fid-t-reference-1": fs.SweepError("down")}),
                            Recorder(), runs=1).attempt(fs.REFERENCE, 1)
        self.assertEqual(record["outcome"], fs.CONTROL_PLANE_ERROR)
        self.assertNotIn("behaviors", record)

    def test_a_non_completed_run_status_is_a_failure_even_on_exit_zero(self):
        evidence = {"fid-t-reference-1": {**healthy([behavior("a")]), "status": "failed"}}
        record = make_sweep(FakeApi(evidence), Recorder(), runs=1).attempt(fs.REFERENCE, 1)
        self.assertEqual(record["outcome"], fs.WORKLOAD_FAILED)

    def test_only_completed_runs_enter_comparisons_and_presence(self):
        attempts = [
            {"side": "reference", "run_id": "r1", "outcome": fs.COMPLETED,
             "behaviors": [behavior("a")], "fingerprint_order": ["a"]},
            {"side": "reference", "run_id": "r2", "outcome": fs.EMPTY_EVIDENCE,
             "behaviors": [], "fingerprint_order": []},
            {"side": "candidate", "run_id": "c1", "outcome": fs.COMPLETED,
             "behaviors": [behavior("a"), behavior("x")], "fingerprint_order": ["a", "x"]},
        ]
        pairs = fs.comparison_pairs(attempts)
        self.assertNotIn("r2", {p[1] for p in pairs} | {p[2] for p in pairs})
        table = fs.presence(attempts)
        self.assertEqual(table["completed_runs"], {"reference": 1, "candidate": 1})


class BoundedExecutionTest(unittest.TestCase):
    def test_consecutive_operational_failures_abort_and_the_rest_are_listed(self):
        rec = Recorder(results={f"fid-t-{s}-{i}": (1, False)
                                for s in fs.SIDES for i in (1, 2, 3)})
        result = make_sweep(FakeApi({}), rec, runs=3, max_consecutive_failures=3).run()
        self.assertEqual(len(rec.calls), 3, "stops executing after the third failure")
        self.assertEqual(len(result["attempts"]), 6, "the unexecuted are still listed")
        self.assertEqual([a["outcome"] for a in result["attempts"][3:]],
                         [fs.NOT_ATTEMPTED] * 3)
        self.assertIsNotNone(result["aborted"])

    def test_evidence_problems_do_not_count_toward_the_abort(self):
        evidence = {f"fid-t-{s}-{i}": {"records": 0, "behaviors": []}
                    for s in fs.SIDES for i in (1, 2)}
        rec = Recorder()
        result = make_sweep(FakeApi(evidence), rec, runs=2, max_consecutive_failures=2).run()
        self.assertEqual(len(rec.calls), 4)
        self.assertIsNone(result["aborted"])

    def test_the_timeout_is_passed_to_every_repetition(self):
        rec = Recorder()
        make_sweep(FakeApi({}), rec, runs=2).run()
        self.assertEqual({c["timeout"] for c in rec.calls}, {900})

    def test_paging_stops_at_a_ceiling_and_on_a_cursor_that_does_not_advance(self):
        api = fs.Api("http://unused")
        api._get = lambda path, query=None: {"behaviors": [], "next_after": "same"}
        with self.assertRaises(fs.SweepError):
            api.behaviors("r")
        counter = {"n": 0}

        def forever(path, query=None):
            counter["n"] += 1
            return {"behaviors": [], "next_after": f"c{counter['n']}"}
        api._get = forever
        with self.assertRaises(fs.SweepError):
            api.behaviors("r")
        self.assertEqual(counter["n"], fs.MAX_PAGES)


class CleanupTest(unittest.TestCase):
    """A real child process: the only way to prove the group is gone."""

    def _alive(self, pid):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    def test_a_timed_out_repetition_leaves_no_process_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = os.path.join(tmp, "child.pid")
            # The leader ignores SIGTERM and has a background child; both must go.
            script = f"trap '' TERM; sleep 300 & echo $! > {pidfile}; wait"
            result = fs.execute(["sh", "-c", script], dict(os.environ), tmp,
                                timeout=1, grace=1)
            self.assertTrue(result["timed_out"])
            child = int(pathlib.Path(pidfile).read_text())
            time.sleep(0.2)
            self.assertFalse(self._alive(child), "the child outlived the repetition")

    def test_a_straggler_left_by_a_normal_exit_is_killed_and_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = os.path.join(tmp, "child.pid")
            script = f"sleep 300 & echo $! > {pidfile}; exit 0"
            result = fs.execute(["sh", "-c", script], dict(os.environ), tmp, timeout=30)
            self.assertFalse(result["timed_out"])
            self.assertEqual(result["returncode"], 0)
            self.assertTrue(result["stragglers"])
            time.sleep(0.2)
            self.assertFalse(self._alive(int(pathlib.Path(pidfile).read_text())))

    def test_child_output_goes_to_the_log_not_anywhere_else(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "run.log")
            fs.execute(["sh", "-c", "echo ticket-text; echo err 1>&2"],
                       dict(os.environ), tmp, timeout=30, log_path=log)
            self.assertEqual(pathlib.Path(log).read_text().split(), ["ticket-text", "err"])


class OutputIntegrityTest(unittest.TestCase):
    def test_a_content_key_anywhere_refuses_the_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp, "r.json")
            with self.assertRaises(fs.SweepError):
                fs.write({"attempts": [{"behaviors": [{"policy_reason": "x"}]}]}, path)
            self.assertFalse(path.exists(), "nothing is published after a refusal")

    def test_the_reader_copies_only_allowlisted_descriptor_fields(self):
        api = fs.Api("http://unused")
        api._get = lambda path, query=None: {"complete": True, "behaviors": [{
            "fingerprint_id": "fp", "observations": "3",
            "behavior": {"operation_category": "tool", "operation_name": "crm_lookup",
                         "target_name": "", "target_category": "", "secret": "no"}}]}
        _, rows = api.behaviors("r")
        self.assertEqual(set(rows[0]), {"fingerprint_id", "observations",
                                        *fs.DESCRIPTOR_FIELDS})

    def test_observation_order_keeps_only_fingerprints_in_sequence_order(self):
        api = fs.Api("http://unused")
        api._get = lambda path, query=None: {"history_state": "complete", "observations": [
            {"sequence": "2", "fingerprint_id": "b", "policy_reason": "free text"},
            {"sequence": "10", "fingerprint_id": "c"},
            {"sequence": "1", "fingerprint_id": "a"}]}
        state, order = api.observation_order("r")
        self.assertEqual((state, order), ("complete", ["a", "b", "c"]))

    def test_written_results_round_trip_and_leave_no_temporary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = fs.write({"attempts": [], "n": 1}, pathlib.Path(tmp, "r.json"))
            self.assertEqual(json.loads(path.read_text()), {"attempts": [], "n": 1})
            self.assertEqual(os.listdir(tmp), ["r.json"])


class AnalysisTest(unittest.TestCase):
    """The offline analysis, against arithmetic known in advance."""

    def attempts(self, ref_sets, cand_sets):
        out = []
        for side, sets in (("reference", ref_sets), ("candidate", cand_sets)):
            for i, s in enumerate(sets, 1):
                out.append({"side": side, "repetition": i, "run_id": f"{side[0]}{i}",
                            "outcome": fs.COMPLETED,
                            "behaviors": [behavior(fp) for fp in sorted(set(s))],
                            "fingerprint_order": list(s)})
        return out

    def test_presence_counts_runs_not_observations(self):
        a = self.attempts([["x", "x", "y"], ["x"]], [["x", "z"]])
        rows = {r["fingerprint_id"]: r for r in fs.presence(a)["identities"]}
        self.assertEqual((rows["x"]["reference_runs_present"], rows["x"]["candidate_runs_present"]), (2, 1))
        self.assertEqual(rows["y"]["reference_runs_present"], 1)
        self.assertEqual(rows["z"]["candidate_runs_present"], 1)

    def test_order_change_without_identity_change_is_counted_as_such(self):
        a = self.attempts([["a", "b"], ["b", "a"], ["a", "b"], ["a", "c"]], [])
        order = fs.order_analysis(a)["reference"]
        self.assertEqual(order["distinct_identity_sets"], 2)
        self.assertEqual(order["distinct_sequences"], 3)
        self.assertEqual(order["pairs_same_set_different_order"], 2)

    def test_k_of_n_split_count_and_threshold(self):
        # 4 runs, groups of 2: C(4,2) * C(2,2) = 6 ordered splits.
        # "rare" in exactly one run: crosses at k=1 whenever it lands in the
        # candidate group and never at k=2.
        a = self.attempts([["x", "rare"], ["x"], ["x"], ["x"]], [])
        result = fs.k_of_n(a, "reference", "reference", 2, [1, 2])
        self.assertEqual([r["splits"] for r in result["rows"]], [6, 6])
        self.assertEqual([r["splits_crossed"] for r in result["rows"]], [3, 0])

    def test_k_of_n_detects_a_behavior_every_candidate_run_added(self):
        a = self.attempts([["x"]] * 3, [["x", "export"]] * 3)
        result = fs.k_of_n(a, "reference", "candidate", 3, [1, 2, 3])
        self.assertEqual([r["splits_crossed"] for r in result["rows"]], [1, 1, 1])

    def test_counted_change_roots_are_reported_per_pair(self):
        comparisons = [
            {"pair": "reference_vs_candidate", "added_count": 2, "added_change_count": 1,
             "correlation_state": "complete", "change_roots": ["tool-export"]},
            {"pair": "reference_vs_candidate", "added_count": 0, "added_change_count": 0,
             "correlation_state": "complete", "change_roots": []},
        ]
        out = fs.change_root_stability(comparisons, "reference_vs_candidate")
        self.assertEqual(out["pairs_with_added"], 1)
        self.assertEqual(out["root_counts"], {"tool-export": 1})
        self.assertEqual(out["added_change_count_distribution"], {"0": 1, "1": 1})


if __name__ == "__main__":
    unittest.main()


class ReportTest(unittest.TestCase):
    """The published tables are a pure function of the results file."""

    def document(self, aborted=None):
        attempts = AnalysisTest().attempts([["a", "b"], ["a"]], [["a", "x"], ["a", "x"]])
        for a in attempts:
            a.update(seconds=1.0, record_count=3, distinct_behavior_count=2,
                     history_state="complete", profile_fresh=True,
                     baseline_files_after=1, stragglers_killed=False)
        attempts.append({"side": "candidate", "repetition": 3, "run_id": "c3",
                         "outcome": fs.NOT_ATTEMPTED, "reason": "aborted"})
        comparisons = [{"pair": "reference_vs_reference", "verdict": "fail",
                        "failed_checks": ["added_behaviors"], "added_count": 1,
                        "added_change_count": 1, "correlation_state": "complete",
                        "change_roots": ["b"], "added_fingerprints": ["b"],
                        "block_decisions_actual": 0, "critical_risk_actual": 0}]
        result = {"attempts": attempts, "aborted": aborted}
        return {"namespace": "n", "aborted": aborted, "attempts": attempts,
                "comparisons": comparisons,
                "method": {"runs_per_side": 2, "temperature": "0.7", "schedule": "s",
                           "learning": "l", "timeout_seconds": 60,
                           "max_consecutive_failures": 3, "gate_limits": {}},
                "provenance": {"trustvian_revision": "r", "demo_commit": "c",
                               "demo_dirty": False, "model": "m", "model_digest": "d" * 20,
                               "model_details": {}, "ollama_version": "v"},
                "analysis": fs.analyze(result, comparisons, 2)}

    def test_rendering_is_deterministic_and_lists_every_attempt(self):
        from tvdemo import fidelity_report
        doc = self.document(aborted="aborted after 3")
        first, second = fidelity_report.render(doc), fidelity_report.render(doc)
        self.assertEqual(first, second)
        self.assertIn("**not_attempted**", first, "an unexecuted repetition is still listed")
        self.assertIn("aborted after 3", first)
        self.assertIn("tool `b`: 1", first)

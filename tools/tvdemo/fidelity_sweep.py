"""The tool-fidelity re-run of Trustvian task 078's measurement.

What this measures, and why it is a second module rather than an edit of
``stability.py``: the first sweep (docs/results/2026-09-27-stability.md) stays
reproducible exactly as it was run. This one differs in ways that change what
its numbers mean, so it is its own code:

  - **Tool-name fidelity.** The workload runs under ``harness/run_agent.py``,
    which emits one GenAI ``execute_tool`` span per dispatch, so a behavior is
    ``tool · crm_lookup`` rather than only ``http · GET → crm.localhost``.
  - **A toolset wider than one run visits.** Eight reference tools, five
    tickets, four tools optional on at least one ticket, one needed by none.
  - **Isolation through ``--behavioral-profile``**, not through a candidate per
    repetition. Trustvian task 078 shipped the flag so a runner can isolate
    learning without changing the identity of the thing under test. Every
    repetition gets a profile nobody has used, and the harness checks the
    baseline file does not already exist before it runs.
  - **Behavior sets from ``GET /v1/evaluation-runs/{id}/behaviors``**, the
    route 078 shipped for exactly this, instead of a self-compare.
  - **Both sides.** Unchanged-versus-unchanged on the reference code, and the
    candidate code (which adds ``export_customer``) as the positive control
    that proves the measurement can detect an added behavior at all.
  - **Every attempt is recorded.** A repetition that fails, times out, or
    produces no or incomplete evidence is an outcome in the results, never
    silently dropped and never counted as a clean empty run.

What this module is **not**: a gate. Every verdict and every per-pair count is
a control-plane response to ``trustvian eval compare``. The presence tables,
order analysis and k-of-N evaluation below are **offline experimental
analysis** over those responses, labelled as such wherever they are reported,
and none of them is a production evaluation rule.
"""

from __future__ import annotations

import datetime
import glob
import itertools
import json
import os
import pathlib
import signal
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

REFERENCE = "reference"
CANDIDATE = "candidate"
SIDES = (REFERENCE, CANDIDATE)

# One outcome per attempted repetition. Only COMPLETED contributes a behavior
# set to the analysis; every other outcome is reported, with its reason, and is
# never treated as a run that happened to observe nothing.
COMPLETED = "completed"
WORKLOAD_FAILED = "workload_failed"          # dev exited non-zero
TIMED_OUT = "timed_out"                      # killed at the repetition timeout
EMPTY_EVIDENCE = "empty_evidence"            # completed, zero records ingested
INCOMPLETE_EVIDENCE = "incomplete_evidence"  # behavior set or history not whole
CONTROL_PLANE_ERROR = "control_plane_error"  # the run could not be read back
ISOLATION_VIOLATION = "isolation_violation"  # the profile already had a baseline
NOT_ATTEMPTED = "not_attempted"              # the sweep was aborted before it
OUTCOMES = (COMPLETED, WORKLOAD_FAILED, TIMED_OUT, EMPTY_EVIDENCE,
            INCOMPLETE_EVIDENCE, CONTROL_PLANE_ERROR, ISOLATION_VIOLATION,
            NOT_ATTEMPTED)

# Outcomes that count toward the consecutive-failure abort: the workload or the
# runtime could not run a repetition. Evidence problems are recorded but do not
# abort, because they are part of what is being measured.
OPERATIONAL = (WORKLOAD_FAILED, TIMED_OUT, CONTROL_PLANE_ERROR,
               ISOLATION_VIOLATION)

RESULTS_SCHEMA_VERSION = "1"

# The only fields copied from a behavior descriptor. Descriptors are
# producer-supplied strings; everything else on an observation — policy_reason
# above all, the one free-text field on the retained row — is never read into
# the results. An allowlist, so a field the server adds later cannot leak in.
DESCRIPTOR_FIELDS = ("operation_category", "operation_name", "target_name",
                     "target_category")

# Page size for the two paged routes. The server's maximum; the harness follows
# next_after until it is absent and never assumes one page is the whole set.
PAGE_LIMIT = 64
# A defensive ceiling on pages per route, so a server that never ends a
# traversal cannot hang the sweep. Retention is bounded at 4096 observations,
# which is 64 pages; one more is the proof that something is wrong.
MAX_PAGES = 65


class SweepError(Exception):
    """The sweep itself cannot continue. Never a measurement result."""


# ---------------------------------------------------------------------------
# Execution: one bounded child process
# ---------------------------------------------------------------------------

def execute(command, env, cwd, timeout, log_path=None, grace=15):
    """Run one repetition's command with a hard wall-clock bound.

    Returns a dict: returncode, timed_out, seconds, and stragglers — whether
    anything the child started was still alive after it exited. The child runs
    in its own session so its whole process group can be signalled:
    ``trustvian dev`` supervises a Collector and the workload, and killing only
    dev would leave both behind holding ports and a baseline lock. On timeout,
    SIGTERM first so dev can fail its run cleanly, then SIGKILL after ``grace``
    seconds. After the leader exits, for any reason, the group is probed and
    anything left is killed: a straggler would contaminate the next repetition.
    """
    started = time.monotonic()
    # The child's output goes to a local log, never into the results: the
    # workload prints ticket text and model decisions, which are content.
    log = open(log_path, "wb") if log_path else subprocess.DEVNULL
    try:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
    finally:
        if log_path:
            log.close()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _signal_group(process, signal.SIGTERM)
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            _signal_group(process, signal.SIGKILL)
            process.wait()
    finally:
        if process.poll() is None:
            _signal_group(process, signal.SIGKILL)
            process.wait()
    stragglers = _group_alive(process)
    if stragglers:
        _signal_group(process, signal.SIGKILL)
    return {"returncode": process.returncode, "timed_out": timed_out,
            "seconds": time.monotonic() - started, "stragglers": stragglers}


def _group_alive(process):
    try:
        os.killpg(process.pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _signal_group(process, sig):
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


# ---------------------------------------------------------------------------
# Reading a run back through /v1
# ---------------------------------------------------------------------------

class Api:
    """GET-only access to the two run-scoped routes the CLI does not wrap.

    Read-only, and it reads through explicit field allowlists: nothing here can
    copy a free-text field into the results.
    """

    def __init__(self, api_url, timeout=30):
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path, query=None):
        url = self.api_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as response:
                return json.loads(response.read())
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise SweepError(f"GET {path} failed: {exc}") from exc

    def _pages(self, path, key):
        after, rows, pages, first = None, [], 0, None
        while True:
            pages += 1
            if pages > MAX_PAGES:
                raise SweepError(f"{path} did not end after {MAX_PAGES} pages")
            query = {"limit": PAGE_LIMIT}
            if after:
                query["after"] = after
            page = self._get(path, query)
            if first is None:
                first = page
            rows.extend(page.get(key) or [])
            nxt = page.get("next_after")
            if not nxt:
                return first, rows
            if nxt == after:
                raise SweepError(f"{path} cursor did not advance past {after!r}")
            after = nxt

    def run(self, run_id):
        return self._get("/v1/evaluation-runs/" + urllib.parse.quote(run_id, safe=""))

    def progress(self, run_id):
        return self._get("/v1/evaluation-runs/"
                         + urllib.parse.quote(run_id, safe="") + "/progress")

    def behaviors(self, run_id):
        first, rows = self._pages(
            "/v1/evaluation-runs/" + urllib.parse.quote(run_id, safe="") + "/behaviors",
            "behaviors")
        return bool(first.get("complete")), [
            {"fingerprint_id": row["fingerprint_id"],
             "observations": int(row["observations"]),
             **{f: (row.get("behavior") or {}).get(f, "") for f in DESCRIPTOR_FIELDS}}
            for row in rows
        ]

    def observation_order(self, run_id):
        """The run's fingerprints in ingest order, and its history state.

        Ingest sequence is the order the platform accepted records, which is
        the only order it records; it is not a claim about wall-clock order.
        Only the fingerprint is kept per observation.
        """
        first, rows = self._pages(
            "/v1/evaluation-runs/" + urllib.parse.quote(run_id, safe="") + "/observations",
            "observations")
        ordered = sorted(rows, key=lambda r: int(r["sequence"]))
        return first.get("history_state", ""), [r["fingerprint_id"] for r in ordered]


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------

def baseline_segment(profile):
    """trustvian dev's filename mapping for a profile (cmd/trustvian/dev_baseline.go).

    Restated rather than imported, because this repository must not import
    Trustvian; a disagreement shows up as an isolation check that never finds
    the file dev wrote, which the post-repetition check below reports.
    """
    out = []
    for ch in profile:
        if ch.isascii() and (ch.isalnum() or ch in "-."):
            out.append(ch)
        else:
            out.extend(f"_{b:02x}" for b in ch.encode())
    return "".join(out)


def baselines_for(profile, state_root):
    pattern = os.path.join(state_root, "*", f"baseline-{baseline_segment(profile)}.json")
    return sorted(glob.glob(pattern))


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------

class Sweep:
    """N repetitions per side, sequential and alternating, each isolated."""

    def __init__(self, spec, root, plane, api, *, runs, model, temperature,
                 timeout, mock_port=None, log_dir=None, namespace=None,
                 state_root=None, max_consecutive_failures=3,
                 executor=execute, clock=None):
        if runs < 1:
            raise ValueError("runs must be at least 1")
        self.spec = spec
        self.root = pathlib.Path(root)
        self.plane = plane
        self.api = api
        self.runs = runs
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self.mock_port = mock_port
        self.log_dir = pathlib.Path(log_dir) if log_dir else None
        self.namespace = namespace or datetime.datetime.now(
            datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.state_root = state_root or os.path.expanduser("~/.trustvian/dev")
        self.max_consecutive_failures = max_consecutive_failures
        self.executor = executor
        self.clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self.bin_dir = self.root / ".demo" / "bin"
        self.venv = self.root / ".demo" / "venv"

    # -- identity ------------------------------------------------------

    def candidate_id(self, side):
        """One candidate per side for the whole sweep: the code under test."""
        return f"fid-{self.namespace}-{side}"

    def profile(self, side, repetition):
        """A learning scope nobody has used. The index is correlation only."""
        return f"fid-{self.namespace}-{side}-p{repetition}"

    def run_id(self, side, repetition):
        return f"fid-{self.namespace}-{side}-{repetition}"

    def schedule(self):
        """Alternating, sequential: r1 c1 r2 c2 … One repetition at a time.

        Alternation is a guard, not a claim: if the model server drifts over a
        two-hour sweep, the drift lands on both sides instead of on whichever
        side ran last.
        """
        return [(side, i) for i in range(1, self.runs + 1) for side in SIDES]

    # -- one repetition ------------------------------------------------

    def command(self, side, repetition):
        spec_side = getattr(self.spec, side)
        return [
            str(self.bin_dir / "trustvian"), "dev",
            "--api-url", self.api.api_url,
            "--project", "support-demo",
            "--agent", "support-agent",
            "--environment", "local",
            "--candidate", self.candidate_id(side),
            "--behavioral-profile", self.profile(side, repetition),
            "--run-id", self.run_id(side, repetition),
            "--instrumentation", self.spec.instrumentation,
            "--",
        ] + [self._resolve(token) for token in spec_side.command]

    def environment(self, side, repetition):
        env = dict(os.environ)
        env.update(getattr(self.spec, side).env)
        if self.mock_port is not None:
            env["SUPPORT_AGENT_PORT"] = str(self.mock_port)
        env["OLLAMA_MODEL"] = self.model
        env["OLLAMA_TEMPERATURE"] = str(self.temperature)
        # Read by the seeded simulation only; the model-driven agent ignores it.
        env["SUPPORT_AGENT_SEED"] = f"{self.namespace}-{side}-{repetition}"
        env.setdefault("PYTHONPATH", str(self.root))
        env["TRUSTVIAN_LOCAL_BIN"] = str(self.bin_dir / "trustvian-local")
        env["TRUSTVIAN_COLLECTOR_BIN"] = str(self.bin_dir / "trustvian-collector")
        return env

    def _resolve(self, token):
        if token == "python":
            return str(self.venv / "bin" / "python")
        if token == "opentelemetry-instrument":
            return str(self.venv / "bin" / "opentelemetry-instrument")
        return token

    def attempt(self, side, repetition):
        """Run one repetition and return its full, privacy-safe record."""
        record = {
            "side": side,
            "repetition": repetition,
            "run_id": self.run_id(side, repetition),
            "candidate": self.candidate_id(side),
            "profile": self.profile(side, repetition),
            "started_at": self.clock().isoformat(),
        }

        existing = baselines_for(record["profile"], self.state_root)
        if existing:
            record.update(outcome=ISOLATION_VIOLATION,
                          reason=f"profile already has {len(existing)} baseline file(s)")
            return record
        record["profile_fresh"] = True

        log_path = None
        if self.log_dir is not None:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            log_path = str(self.log_dir / f"{record['run_id']}.log")
        result = self.executor(
            self.command(side, repetition), self.environment(side, repetition),
            str(self.root), self.timeout, log_path)
        returncode, timed_out = result["returncode"], result["timed_out"]
        record.update(exit_code=returncode, seconds=round(result["seconds"], 1),
                      timed_out=timed_out, stragglers_killed=result["stragglers"])
        record["baseline_files_after"] = len(baselines_for(record["profile"], self.state_root))

        if timed_out:
            record.update(outcome=TIMED_OUT,
                          reason=f"killed at the {self.timeout}s repetition timeout")
        elif returncode != 0:
            record.update(outcome=WORKLOAD_FAILED,
                          reason=f"trustvian dev exited {returncode}")
        try:
            record.update(self._evidence(record["run_id"]))
        except SweepError as exc:
            if "outcome" not in record:
                record.update(outcome=CONTROL_PLANE_ERROR, reason=str(exc))
            return record

        if "outcome" not in record:
            if record["status"] != "completed":
                record.update(outcome=WORKLOAD_FAILED,
                              reason=f"run status is {record['status']!r}")
            elif record["record_count"] == 0:
                record.update(outcome=EMPTY_EVIDENCE,
                              reason="the run completed with zero records")
            elif not (record["behavior_complete"] and record["behaviors_complete"]
                      and record["history_state"] == "complete"):
                record.update(outcome=INCOMPLETE_EVIDENCE,
                              reason="behavior set or retained history is not complete")
            else:
                record["outcome"] = COMPLETED
        return record

    def _evidence(self, run_id):
        run = self.api.run(run_id)
        progress = self.api.progress(run_id)
        complete, behaviors = self.api.behaviors(run_id)
        history_state, order = self.api.observation_order(run_id)
        return {
            "status": run.get("status", ""),
            "record_count": int(progress.get("record_count", "0")),
            "distinct_behavior_count": int(progress.get("distinct_behavior_count", 0)),
            "behavior_complete": bool(progress.get("behavior_complete")),
            "behaviors_complete": complete,
            "history_state": history_state,
            "behaviors": sorted(behaviors, key=lambda b: b["fingerprint_id"]),
            "fingerprint_order": order,
        }

    # -- the sweep -----------------------------------------------------

    def run(self, on_attempt=None):
        attempts, consecutive, aborted = [], 0, None
        for side, repetition in self.schedule():
            if aborted:
                attempts.append({"side": side, "repetition": repetition,
                                 "run_id": self.run_id(side, repetition),
                                 "outcome": NOT_ATTEMPTED, "reason": aborted})
                continue
            record = self.attempt(side, repetition)
            attempts.append(record)
            if on_attempt:
                on_attempt(record)
            if record["outcome"] in OPERATIONAL:
                consecutive += 1
                if consecutive >= self.max_consecutive_failures:
                    aborted = (f"sweep aborted after {consecutive} consecutive "
                               f"operational failures")
            else:
                consecutive = 0
        return {"namespace": self.namespace, "attempts": attempts,
                "aborted": aborted}


# ---------------------------------------------------------------------------
# Server comparisons
# ---------------------------------------------------------------------------

def comparison_pairs(attempts):
    """The pairs to compare, from completed repetitions only.

    reference×reference and candidate×candidate: every ordered pair — the
    unchanged-versus-unchanged question, asked of each code version. And
    reference×candidate: every pair — the positive control.
    """
    done = {side: [a["run_id"] for a in attempts
                   if a["side"] == side and a["outcome"] == COMPLETED]
            for side in SIDES}
    pairs = []
    for label, left, right in (("reference_vs_reference", REFERENCE, REFERENCE),
                               ("candidate_vs_candidate", CANDIDATE, CANDIDATE),
                               ("reference_vs_candidate", REFERENCE, CANDIDATE)):
        if left == right:
            combos = itertools.permutations(done[left], 2)
        else:
            combos = itertools.product(done[left], done[right])
        pairs.extend((label, a, b) for a, b in combos)
    return pairs


def summarize_comparison(label, reference_run, candidate_run, status, payload):
    """One control-plane comparison, reduced to its privacy-safe numbers."""
    gate = payload["gate"]
    diff = payload["behavior_diff"]
    checks = ("reference_evidence", "candidate_evidence", "added_behaviors",
              "block_decisions", "critical_risk_observations")
    changes = gate.get("added_behavior_changes") or {}
    return {
        "pair": label,
        "reference_run": reference_run,
        "candidate_run": candidate_run,
        "exit_code": status,
        "verdict": gate["verdict"],
        "failed_checks": [c for c in checks if not gate[c]["passed"]]
        + (["added_behavior_changes"]
           if changes.get("state") == "evaluated" and not changes.get("passed")
           else []),
        "added_count": int(diff["added_count"]),
        "removed_count": int(diff["removed_count"]),
        "added_change_count": int(diff.get("added_change_count", 0)),
        "correlation_state": diff.get("correlation_state", ""),
        "counting_policy_version": diff.get("counting_policy_version", ""),
        "change_roots": sorted(c["root_fingerprint_id"]
                               for c in diff.get("added_changes") or []),
        "added_fingerprints": sorted(d["fingerprint_id"] for d in diff.get("deltas", [])
                                     if d.get("change") == "added"),
        "removed_fingerprints": sorted(d["fingerprint_id"] for d in diff.get("deltas", [])
                                       if d.get("change") == "removed"),
        "change_check_state": changes.get("state", ""),
        "block_decisions_actual": int(gate["block_decisions"]["actual"]),
        "critical_risk_actual": int(gate["critical_risk_observations"]["actual"]),
    }


def compare_all(plane, attempts, limits):
    out = []
    for label, reference_run, candidate_run in comparison_pairs(attempts):
        status, payload = plane.compare(reference_run, candidate_run, limits)
        out.append(summarize_comparison(label, reference_run, candidate_run,
                                        status, payload))
    return out


# ---------------------------------------------------------------------------
# Offline experimental analysis — never a production rule
# ---------------------------------------------------------------------------

def descriptors(attempts):
    """fingerprint → descriptor, from the behavior sets the server returned."""
    out = {}
    for a in attempts:
        for b in a.get("behaviors", []):
            out.setdefault(b["fingerprint_id"],
                           {f: b[f] for f in DESCRIPTOR_FIELDS})
    return out


def presence(attempts):
    """Per identity: in how many completed runs of each side it appeared."""
    done = [a for a in attempts if a["outcome"] == COMPLETED]
    n = {side: sum(1 for a in done if a["side"] == side) for side in SIDES}
    table = {}
    for a in done:
        for b in a["behaviors"]:
            row = table.setdefault(b["fingerprint_id"],
                                   {"fingerprint_id": b["fingerprint_id"],
                                    **{f: b[f] for f in DESCRIPTOR_FIELDS},
                                    "reference_runs_present": 0,
                                    "candidate_runs_present": 0})
            row[f"{a['side']}_runs_present"] += 1
    rows = sorted(table.values(), key=lambda r: (
        r["operation_category"], r["operation_name"], r["target_name"]))
    return {"completed_runs": n, "identities": rows}


def order_analysis(attempts, category="tool"):
    """Action order versus identity set, per side, at one category.

    The sequence of tool identities in ingest order, and the set of them. Two
    runs with the same set and a different sequence changed order without
    changing identity — which no presence count and no diff sees, by design.
    """
    desc = descriptors(attempts)
    out = {}
    for side in SIDES:
        runs = [a for a in attempts if a["side"] == side and a["outcome"] == COMPLETED]
        sequences, sets = [], []
        for a in runs:
            seq = tuple(fp for fp in a["fingerprint_order"]
                        if desc.get(fp, {}).get("operation_category") == category)
            sequences.append(seq)
            sets.append(frozenset(seq))
        same_set_different_order = sum(
            1 for i, j in itertools.combinations(range(len(runs)), 2)
            if sets[i] == sets[j] and sequences[i] != sequences[j])
        out[side] = {
            "runs": len(runs),
            "distinct_identity_sets": len(set(sets)),
            "distinct_sequences": len(set(sequences)),
            "unordered_pairs": len(runs) * (len(runs) - 1) // 2,
            "pairs_same_set_different_order": same_set_different_order,
            "sequence_lengths": sorted(len(s) for s in sequences),
        }
    return out


def k_of_n(attempts, side_a, side_b, group_size, ks, j=0, category=None):
    """What task 078's repeated-added rule would have reported, offline.

    For every split of side_a's completed runs into a reference group and
    side_b's into a candidate group (disjoint when the sides are the same), a
    behavior is repeatedly added when candidate_runs_present >= k and
    reference_runs_present <= j. Reported per k as the number of splits in
    which at least one behavior crossed. This is **offline experimental
    analysis** of 078's specified per-identity rule over server-returned
    behavior sets, not the platform's aggregation and not a recommendation.
    """
    desc = descriptors(attempts)

    def sets_for(side):
        return [
            frozenset(b["fingerprint_id"] for b in a["behaviors"]
                      if category is None or desc[b["fingerprint_id"]]["operation_category"] == category)
            for a in attempts if a["side"] == side and a["outcome"] == COMPLETED]

    a_sets = sets_for(side_a)
    b_sets = a_sets if side_a == side_b else sets_for(side_b)
    splits = []
    if side_a == side_b:
        if 2 * group_size > len(a_sets):
            return {"error": f"needs {2 * group_size} completed runs, have {len(a_sets)}"}
        for ref in itertools.combinations(range(len(a_sets)), group_size):
            rest = [i for i in range(len(a_sets)) if i not in ref]
            for cand in itertools.combinations(rest, group_size):
                splits.append(([a_sets[i] for i in ref], [a_sets[i] for i in cand]))
    else:
        if group_size > min(len(a_sets), len(b_sets)):
            return {"error": f"needs {group_size} completed runs per side"}
        for ref in itertools.combinations(range(len(a_sets)), group_size):
            for cand in itertools.combinations(range(len(b_sets)), group_size):
                splits.append(([a_sets[i] for i in ref], [b_sets[i] for i in cand]))

    rows = []
    for k in ks:
        crossed = 0
        for ref_group, cand_group in splits:
            universe = frozenset().union(*ref_group, *cand_group)
            if any(sum(fp in s for s in cand_group) >= k
                   and sum(fp in s for s in ref_group) <= j for fp in universe):
                crossed += 1
        rows.append({"k": k, "j": j, "splits": len(splits), "splits_crossed": crossed})
    return {"group_size": group_size, "category": category or "all", "rows": rows}


def change_root_stability(comparisons, label):
    """How often each fingerprint was a counted-change root across pairs.

    Counted changes (ADR 0052) are computed per pair, from that pair's added
    set and the candidate run's recorded parentage. A root is therefore a
    property of a *pair*, not a stable identity: this reports how many of the
    pairs with any added behavior named each fingerprint as a root.
    """
    pairs = [c for c in comparisons if c["pair"] == label]
    with_added = [c for c in pairs if c["added_count"] > 0]
    counts = {}
    for c in with_added:
        for root in c["change_roots"]:
            counts[root] = counts.get(root, 0) + 1
    return {
        "pairs": len(pairs),
        "pairs_with_added": len(with_added),
        "added_count_distribution": _distribution(c["added_count"] for c in pairs),
        "added_change_count_distribution": _distribution(c["added_change_count"] for c in pairs),
        "correlation_states": _distribution(c["correlation_state"] for c in pairs),
        "root_counts": dict(sorted(counts.items())),
    }


def _distribution(values):
    out = {}
    for v in values:
        out[str(v)] = out.get(str(v), 0) + 1
    return dict(sorted(out.items(), key=lambda kv: kv[0]))


def verdict_table(comparisons):
    """Server verdicts per pair type, with the checks that failed separated."""
    out = {}
    for c in comparisons:
        row = out.setdefault(c["pair"], {"total": 0, "fail": 0, "failed_checks": {}})
        row["total"] += 1
        if c["verdict"] == "fail":
            row["fail"] += 1
        for check in c["failed_checks"]:
            row["failed_checks"][check] = row["failed_checks"].get(check, 0) + 1
    return out


def outcome_table(attempts):
    out = {side: {o: 0 for o in OUTCOMES} for side in SIDES}
    for a in attempts:
        out[a["side"]][a["outcome"]] += 1
    return out


def analyze(sweep_result, comparisons, runs, category="tool"):
    attempts = sweep_result["attempts"]
    group = runs // 2
    ks = list(range(1, group + 1))
    return {
        "label": "offline experimental analysis over server responses; not a gate",
        "outcomes": outcome_table(attempts),
        "verdicts": verdict_table(comparisons),
        "presence": presence(attempts),
        "order": order_analysis(attempts, category),
        "k_of_n": {
            "reference_vs_reference": k_of_n(attempts, REFERENCE, REFERENCE, group, ks),
            "reference_vs_reference_tool_only":
                k_of_n(attempts, REFERENCE, REFERENCE, group, ks, category=category),
            "reference_vs_candidate": k_of_n(attempts, REFERENCE, CANDIDATE, group, ks),
        },
        "counted_changes": {
            label: change_root_stability(comparisons, label)
            for label in ("reference_vs_reference", "candidate_vs_candidate",
                          "reference_vs_candidate")
        },
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def assert_privacy_safe(document):
    """Refuse to write a results document carrying any content-bearing key.

    The results hold identifiers, descriptors, counts, states and verdicts. A
    key naming content — however it got there — means something upstream of
    this function stopped using the allowlists, and the file must not be
    written.
    """
    forbidden = {"policy_reason", "prompt", "completion", "content", "messages",
                 "arguments", "body", "email_body", "email_subject", "query",
                 "reason_text", "attributes", "events"}

    def walk(value, path):
        if isinstance(value, dict):
            for key, inner in value.items():
                if key in forbidden:
                    raise SweepError(f"results carry a content key at {path}.{key}")
                walk(inner, f"{path}.{key}")
        elif isinstance(value, list):
            for i, inner in enumerate(value):
                walk(inner, f"{path}[{i}]")

    walk(document, "$")


def write(document, path):
    assert_privacy_safe(document)
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    # Round-trip before publishing: a truncated or unparsable file never takes
    # the final name.
    json.loads(tmp.read_text())
    tmp.replace(path)
    return path

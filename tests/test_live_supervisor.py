#!/usr/bin/env python3
"""live.py, the supervisor, with stubs instead of `trustvian dev` and the agent. No model, no Trustvian."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import roles  # noqa: E402

STUB = [sys.executable, str(ROOT / "tests" / "live_stub_worker.py")]
STUB_TRUSTVIAN = ROOT / "tests" / "live_stub_trustvian"
TICKET = re.compile(r"^(\S+-agent)\s+ticket (\d+)\s+tools: (.*?)  \(")
RUN = re.compile(r"^(\S+-agent)\s+run (\S+)$")
ROLES = ("support", "billing", "accounts")
API_URL = "http://127.0.0.1:65530"


def tickets(stdout: str) -> list:
    """(actor, ticket number, [tools]) per ticket line."""
    found = []
    for line in stdout.splitlines():
        match = TICKET.match(line)
        if match:
            found.append((match[1], int(match[2]), match[3].split(", ")))
    return found


def runs(stdout: str) -> list:
    """(actor, run id) per run line, in order."""
    return [m.groups() for m in map(RUN.match, stdout.splitlines()) if m]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                           capture_output=True, text=True).stdout.strip()
    return state != "" and not state.startswith("Z")


class Supervisor(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def environment(self, **env) -> dict:
        return {**os.environ, "PATH": f"{STUB_TRUSTVIAN}{os.pathsep}{os.environ['PATH']}",
                "TRUSTVIAN_API_URL": API_URL, "STUB_DIR": str(self.dir),
                "LIVE_INTERVAL_SECONDS": "0", **env}

    def live(self, timeout=30, **env):
        done = subprocess.run([sys.executable, str(ROOT / "live.py"), "--", *STUB],
                              env=self.environment(**env), capture_output=True, text=True,
                              timeout=timeout)
        return done.returncode, done.stdout

    def dev_args(self, actor: str) -> list:
        """The arguments of each `trustvian dev` started for actor, in order."""
        return [json.loads(line) for line in (self.dir / f"{actor}.dev").read_text().splitlines()]

    def start_forever(self, **env):
        proc = subprocess.Popen([sys.executable, str(ROOT / "live.py"), "--", *STUB],
                                env=self.environment(LIVE_INTERVAL_SECONDS="0.2", **env),
                                stdout=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if all((self.dir / f"{r}.pid").exists() for r in ROLES):
                break
            time.sleep(0.1)
        time.sleep(1)
        pids = [int(p) for r in ROLES for p in (self.dir / f"{r}.pid").read_text().split()]
        pids += [int(p) for r in ROLES for p in (self.dir / f"{r}-agent.devpid").read_text().split()]
        return proc, pids

    def test_one_dev_per_role_each_its_own_run(self):
        status, out = self.live(LIVE_MAX_TICKETS="1")
        self.assertEqual(status, 0, out)
        for role in ROLES:
            actor = f"{role}-agent"
            self.assertEqual(self.dev_args(actor), [[
                "--api-url", API_URL, "--project", "support-demo",
                "--agent", actor, "--candidate", f"live-{role}", "--instrumentation", "existing"]])
            # LIVE_ROLE picked the role; dev's --agent became OTEL_SERVICE_NAME.
            self.assertEqual((self.dir / f"{role}.service").read_text().split(), [actor])
        found = dict(runs(out))
        self.assertEqual(sorted(found), ["accounts-agent", "billing-agent", "support-agent"])
        for actor, run in found.items():
            self.assertTrue(run.startswith(f"dev-live-{actor.split('-')[0]}-"), run)
            self.assertIn(f"{actor:<15} run {run} complete", out)
        self.assertEqual(sorted(a for a, _, _ in tickets(out)),
                         ["accounts-agent", "billing-agent", "support-agent"])

    def test_the_project_comes_from_trustvian_project(self):
        status, out = self.live(LIVE_MAX_TICKETS="1", LIVE_AGENTS="support", TRUSTVIAN_PROJECT="other")
        self.assertEqual(status, 0, out)
        args = self.dev_args("support-agent")[0]
        self.assertEqual(args[args.index("--project") + 1], "other")

    def test_without_trustvian_api_url_it_refuses_and_names_the_control_plane_command(self):
        env = self.environment()
        del env["TRUSTVIAN_API_URL"]
        done = subprocess.run([sys.executable, str(ROOT / "live.py"), "--", *STUB],
                              env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(done.returncode, 2)
        self.assertEqual(done.stdout, "")
        self.assertEqual(done.stderr.count("\n"), 1)
        self.assertIn("TRUSTVIAN_API_URL", done.stderr)
        self.assertIn(".demo/bin/trustvian-local --state-dir .demo/live --listen 127.0.0.1:8080", done.stderr)
        self.assertEqual(list(self.dir.iterdir()), [])  # no dev was started

    def test_live_agents_selects_the_roles(self):
        status, out = self.live(LIVE_MAX_TICKETS="1", LIVE_AGENTS="billing")
        self.assertEqual(status, 0, out)
        self.assertEqual([a for a, _, _ in tickets(out)], ["billing-agent"])
        self.assertFalse((self.dir / "support-agent.dev").exists())

    def test_max_tickets_is_per_agent_and_ends_every_run(self):
        status, out = self.live(LIVE_MAX_TICKETS="3")
        self.assertEqual(status, 0, out)
        for role in ROLES:
            self.assertEqual([n for a, n, _ in tickets(out) if a == f"{role}-agent"], [1, 2, 3])
        self.assertEqual(out.count(" complete\n"), 3)
        self.assertNotIn("restarting", out)

    def test_the_role_binds_its_own_tools(self):
        _, out = self.live(LIVE_MAX_TICKETS="1", LIVE_DRIFT_EVERY="0")
        self.assertEqual({a: t for a, _, t in tickets(out)}, {
            "support-agent": ["lookup_customer", "send_reply"],
            "billing-agent": ["lookup_customer", "lookup_invoice", "issue_refund"],
            "accounts-agent": ["lookup_customer", "change_plan"],
        })

    def test_drift_every_nth_billing_ticket_only(self):
        status, out = self.live(LIVE_MAX_TICKETS="6", LIVE_DRIFT_EVERY="3")
        self.assertEqual(status, 0, out)
        drifted = [(a, n) for a, n, t in tickets(out) if "export_customer" in t]
        self.assertEqual(drifted, [("billing-agent", 3), ("billing-agent", 6)])

    def test_drift_off(self):
        status, out = self.live(LIVE_MAX_TICKETS="4", LIVE_DRIFT_EVERY="0", LIVE_AGENTS="billing")
        self.assertEqual(status, 0, out)
        self.assertTrue(all("export_customer" not in t for _, _, t in tickets(out)))

    def test_a_crashed_agent_is_restarted_as_a_new_run_and_carries_on(self):
        status, out = self.live(LIVE_MAX_TICKETS="4", STUB_CRASH_AT="2")
        self.assertEqual(status, 0, out)
        self.assertIn("live: billing-agent's dev exited with status 1; restarting in 2 s", out)
        self.assertIn("live: restarting billing-agent (restart 1)", out)
        # The ticket that crashed is worked again by the new run; the total holds.
        for role in ROLES:
            self.assertEqual([n for a, n, _ in tickets(out) if a == f"{role}-agent"], [1, 2, 3, 4])
        self.assertEqual(len(self.dev_args("billing-agent")), 2)
        billing = [run for actor, run in runs(out) if actor == "billing-agent"]
        self.assertEqual(len(set(billing)), 2)

    def test_a_dev_that_cannot_start_is_retried(self):
        status, out = self.live(LIVE_MAX_TICKETS="1", LIVE_AGENTS="support", STUB_DEV_FAIL_ONCE="1")
        self.assertEqual(status, 0, out)
        self.assertIn("support-agent   dev: creating the project: already_exists", out)
        self.assertIn("live: support-agent's dev exited with status 3; restarting in 2 s", out)
        self.assertEqual([n for _, n, _ in tickets(out)], [1])

    def test_sigterm_completes_every_run_and_leaves_nothing_behind(self):
        proc, pids = self.start_forever(STUB_GRANDCHILD="1")
        self.assertEqual(len(pids), 9)  # 3 devs, 3 agents, 3 grandchildren
        self.assertTrue(all(alive(p) for p in pids))
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0, out)
        self.assertLess(time.monotonic() - started, 3)
        self.assertIn("live: stopping; each dev completes its run", out)
        self.assertEqual(out.count(" complete\n"), 3)
        time.sleep(0.2)
        self.assertEqual([p for p in pids if alive(p)], [])

    def test_a_dev_that_ignores_sigint_is_killed_with_its_agent_after_the_grace_period(self):
        proc, pids = self.start_forever(STUB_DEV_IGNORE_INT="1", STUB_GRANDCHILD="1",
                                        LIVE_STOP_GRACE_SECONDS="2")
        started = time.monotonic()
        proc.send_signal(signal.SIGINT)
        out, _ = proc.communicate(timeout=15)
        took = time.monotonic() - started
        self.assertEqual(proc.returncode, 0, out)
        self.assertTrue(1.5 < took < 6, took)
        self.assertIn("did not finish within 2 s; killing it", out)
        time.sleep(0.2)
        self.assertEqual([p for p in pids if alive(p)], [])

    def test_ticket_lines_carry_no_ticket_text(self):
        status, out = self.live(LIVE_MAX_TICKETS="8")
        self.assertEqual(status, 0)
        for spec in roles.ROLES.values():
            for _, text in spec["tickets"]:
                self.assertNotIn(text, out)


if __name__ == "__main__":
    unittest.main()

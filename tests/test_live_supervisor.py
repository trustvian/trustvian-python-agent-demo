#!/usr/bin/env python3
"""live.py, the supervisor, with a stub worker instead of the agent. No model, no Trustvian."""

from __future__ import annotations

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
TICKET = re.compile(r"^(\S+-agent)\s+ticket (\d+)\s+tools: (.*?)  \(")
ROLES = ("support", "billing", "accounts")


def tickets(stdout: str) -> list:
    """(actor, ticket number, [tools]) per ticket line."""
    found = []
    for line in stdout.splitlines():
        match = TICKET.match(line)
        if match:
            found.append((match[1], int(match[2]), match[3].split(", ")))
    return found


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

    def live(self, timeout=30, **env):
        environment = {**os.environ, "STUB_DIR": str(self.dir), "LIVE_INTERVAL_SECONDS": "0", **env}
        done = subprocess.run([sys.executable, str(ROOT / "live.py"), "--", *STUB],
                              env=environment, capture_output=True, text=True, timeout=timeout)
        return done.returncode, done.stdout

    def start_forever(self, **env):
        environment = {**os.environ, "STUB_DIR": str(self.dir), "LIVE_INTERVAL_SECONDS": "0.2", **env}
        proc = subprocess.Popen([sys.executable, str(ROOT / "live.py"), "--", *STUB],
                                env=environment, stdout=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if all((self.dir / f"{r}.pid").exists() for r in ROLES):
                break
            time.sleep(0.1)
        time.sleep(1)
        pids = [int(p) for r in ROLES for p in (self.dir / f"{r}.pid").read_text().split()]
        return proc, pids

    def test_one_worker_per_role_with_its_own_service_name(self):
        status, out = self.live(LIVE_MAX_TICKETS="1")
        self.assertEqual(status, 0, out)
        for role in ROLES:
            self.assertEqual((self.dir / f"{role}.service").read_text().split(), [f"{role}-agent"])
        self.assertEqual(sorted(a for a, _, _ in tickets(out)),
                         ["accounts-agent", "billing-agent", "support-agent"])

    def test_live_agents_selects_the_roles(self):
        status, out = self.live(LIVE_MAX_TICKETS="1", LIVE_AGENTS="billing")
        self.assertEqual(status, 0, out)
        self.assertEqual([a for a, _, _ in tickets(out)], ["billing-agent"])

    def test_max_tickets_is_per_worker_and_ends_the_run(self):
        status, out = self.live(LIVE_MAX_TICKETS="3")
        self.assertEqual(status, 0, out)
        for role in ROLES:
            self.assertEqual([n for a, n, _ in tickets(out) if a == f"{role}-agent"], [1, 2, 3])

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

    def test_a_crashed_worker_is_restarted_and_carries_on(self):
        status, out = self.live(LIVE_MAX_TICKETS="4", STUB_CRASH_AT="2")
        self.assertEqual(status, 0, out)
        self.assertIn("live: billing-agent exited with status 1; restarting in 2 s", out)
        self.assertIn("live: restarting billing-agent (restart 1)", out)
        for role in ROLES:
            self.assertEqual([n for a, n, _ in tickets(out) if a == f"{role}-agent"], [1, 2, 3, 4])

    def test_sigterm_stops_every_worker_and_leaves_nothing_behind(self):
        proc, pids = self.start_forever(STUB_GRANDCHILD="1")
        self.assertEqual(len(pids), 6)
        self.assertTrue(all(alive(p) for p in pids))
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, out)
        self.assertLess(time.monotonic() - started, 3)
        time.sleep(0.2)
        self.assertEqual([p for p in pids if alive(p)], [])

    def test_a_worker_that_ignores_sigterm_is_killed_after_the_grace_period(self):
        proc, pids = self.start_forever(STUB_IGNORE_TERM="1")
        started = time.monotonic()
        proc.send_signal(signal.SIGINT)
        out, _ = proc.communicate(timeout=15)
        took = time.monotonic() - started
        self.assertEqual(proc.returncode, 0, out)
        self.assertTrue(4.5 < took < 8, took)
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

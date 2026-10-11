"""Live mode: a small fleet of agents that keeps working until stopped. Standard library only.

Imports neither OpenTelemetry nor Trustvian, nor the agent. It starts one
worker per role in LIVE_AGENTS, each the instrumented agent exactly as every
other run here starts it (`opentelemetry-instrument python
harness/run_agent.py`, from the interpreter live.py runs in), with

  OTEL_SERVICE_NAME=<role>-agent   so Trustvian sees one actor per role
  LIVE_ROLE=<role>                 so the agent works that role's tickets

and everything else inherited, including the OTLP endpoint and resource
attributes `trustvian dev` set, and SUPPORT_AGENT_PORT. The agents and their
tickets are in agent/roles.py. `make live` runs it:

  trustvian dev --instrumentation existing --agent support-fleet \\
    --candidate live -- .demo/venv/bin/python live.py

Ctrl-C (or SIGTERM) stops every worker, SIGTERM first and SIGKILL after 5 s,
and exits 0, so dev completes the run. A worker that crashes is restarted
after a back-off; the others keep working.

  LIVE_AGENTS             support,billing,accounts
  LIVE_INTERVAL_SECONDS   10   one ticket per worker this often, jittered +-50%
  LIVE_DRIFT_EVERY        20   every Nth billing ticket also exports; 0 = never
  LIVE_MAX_TICKETS        0    tickets per worker, then stop; 0 = forever
  CRM_ERROR_RATE          read by mock_services/server.py, not here

Arguments after `--` replace the worker command (the tests use a stub):
  python live.py -- python tests/stub_worker.py
"""
import os
import queue
import signal
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROLES = ("support", "billing", "accounts")
BIN = os.path.dirname(sys.executable)
WORKER = [os.path.join(BIN, "opentelemetry-instrument"), sys.executable, "harness/run_agent.py"]
STOP_GRACE_SECONDS = 5
BACKOFF_SECONDS = (2, 4, 8, 16, 30)


class Worker:
    def __init__(self, role: str, command: list, max_tickets: int):
        self.role = role
        self.actor = f"{role}-agent"
        self.command = command
        self.max_tickets = max_tickets
        self.handled = 0  # tickets reported, across restarts
        self.crashes = 0  # consecutive crashes without a ticket in between
        self.restarts = 0
        self.proc = None
        self.reader = None
        self.restart_at = None
        self.done = False

    def start(self, lines: queue.Queue) -> None:
        env = dict(os.environ)
        env["OTEL_SERVICE_NAME"] = self.actor
        env["LIVE_ROLE"] = self.role
        # A restarted worker carries on numbering, and stops at the same total.
        env["LIVE_FIRST_TICKET"] = str(self.handled + 1)
        if self.max_tickets > 0:
            env["LIVE_MAX_TICKETS"] = str(self.max_tickets - self.handled)
        self.proc = subprocess.Popen(
            self.command,
            cwd=HERE,
            env=env,
            stdout=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            # Its own process group: Ctrl-C reaches live.py only, which then
            # stops each worker together with anything it started.
            start_new_session=True,
        )
        self.reader = threading.Thread(target=self._read, args=(self.proc, lines), daemon=True)
        self.reader.start()
        self.restart_at = None

    def _read(self, proc, lines: queue.Queue) -> None:
        # Only the per-ticket lines are kept. Everything else the agent prints
        # (framework logs, its answers) is dropped: it may quote a ticket.
        for line in proc.stdout:
            if line.startswith("@live "):
                lines.put((self, line))
        proc.stdout.close()

    def signal(self, sig: int) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, sig)
            except ProcessLookupError:
                pass


def parse(line: str) -> dict:
    fields = dict(part.split("=", 1) for part in line.split()[1:] if "=" in part)
    tools = fields.get("tools", "-")
    fields["tools"] = [] if tools == "-" else tools.split(",")
    return fields


def show(worker: Worker, fields: dict) -> None:
    tools = ", ".join(fields["tools"]) or "none"
    line = f"{worker.actor:<15} ticket {fields.get('ticket', '?'):<4} tools: {tools}  ({fields.get('seconds', '?')} s)"
    if fields.get("failed", "0") != "0":
        line += f"  failed calls: {fields['failed']}"
    if fields.get("drift") == "1":
        line += "  [drift]"
    if fields.get("error"):
        line += f"  error: {fields['error']}"
    print(line, flush=True)


def main(argv: list) -> int:
    command = argv[argv.index("--") + 1:] if "--" in argv else WORKER
    roles = [r.strip() for r in os.environ.get("LIVE_AGENTS", ",".join(ROLES)).split(",") if r.strip()]
    unknown = [r for r in roles if r not in ROLES]
    if not roles or unknown:
        print(f"live: LIVE_AGENTS must name some of {', '.join(ROLES)}; got {', '.join(unknown) or 'none'}",
              file=sys.stderr)
        return 2
    max_tickets = int(os.environ.get("LIVE_MAX_TICKETS", "0"))
    interval = os.environ.get("LIVE_INTERVAL_SECONDS", "10")

    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())

    lines = queue.Queue()
    workers = [Worker(role, command, max_tickets) for role in roles]
    print(f"live: {len(workers)} agents ({', '.join(w.actor for w in workers)}), "
          f"one ticket each every ~{interval} s"
          + (f", {max_tickets} each" if max_tickets > 0 else ", until stopped (Ctrl-C)"), flush=True)
    for worker in workers:
        worker.start(lines)

    while not stopping.is_set() and not all(w.done for w in workers):
        try:
            worker, line = lines.get(timeout=0.2)
            worker.handled += 1
            worker.crashes = 0
            show(worker, parse(line))
            continue
        except queue.Empty:
            pass
        now = time.monotonic()
        for worker in workers:
            if worker.done:
                continue
            if worker.restart_at is not None:
                if now >= worker.restart_at:
                    worker.restarts += 1
                    print(f"live: restarting {worker.actor} (restart {worker.restarts})", flush=True)
                    worker.start(lines)
                continue
            status = worker.proc.poll()
            if status is None:
                continue
            worker.reader.join(timeout=1)  # its last lines are queued before it counts as gone
            if not lines.empty():
                continue  # count them first, then decide
            if max_tickets > 0 and worker.handled >= max_tickets:
                worker.done = True
                continue
            delay = BACKOFF_SECONDS[min(worker.crashes, len(BACKOFF_SECONDS) - 1)]
            worker.crashes += 1
            worker.restart_at = now + delay
            print(f"live: {worker.actor} exited with status {status}; restarting in {delay} s",
                  flush=True)

    if stopping.is_set():
        print("live: stopping", flush=True)
    stop(workers)
    while not lines.empty():
        worker, line = lines.get()
        worker.handled += 1
        show(worker, parse(line))
    print("live: " + ", ".join(f"{w.actor} {w.handled} tickets" for w in workers), flush=True)
    return 0


def stop(workers: list) -> None:
    """SIGTERM every worker's process group, SIGKILL what is left after the grace period."""
    for worker in workers:
        worker.signal(signal.SIGTERM)
    deadline = time.monotonic() + STOP_GRACE_SECONDS
    for worker in workers:
        if worker.proc is None:
            continue
        try:
            worker.proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
    for worker in workers:
        if worker.proc is None:
            continue
        # The whole group, even when the worker itself has exited: nothing it
        # started may outlive it.
        try:
            os.killpg(worker.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        worker.proc.wait()
        if worker.reader:
            worker.reader.join(timeout=1)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Live mode: a small fleet of agents that keeps working until stopped. Standard library only.

Imports neither OpenTelemetry nor Trustvian, nor the agent. Every role in
LIVE_AGENTS is its own Trustvian run: live.py starts one `trustvian dev` per
role, all attached to one control plane that is already running,

  trustvian dev --api-url $TRUSTVIAN_API_URL --project support-demo \\
    --agent <role>-agent --candidate live-<role> \\
    --instrumentation existing -- opentelemetry-instrument python harness/run_agent.py

that is, the instrumented agent exactly as every other run here starts it
(from the interpreter live.py runs in), with LIVE_ROLE=<role> in its
environment, so the agent works that role's tickets, and everything else
inherited, SUPPORT_AGENT_PORT included. dev exports --agent as
OTEL_SERVICE_NAME and starts a Collector of its own; the live view lists each
run as its own card. The candidate is per role: a candidate belongs to one
agent (one shared by three would file all three runs under the first agent),
and it is dev's learning scope, so each role learns into a baseline of its
own. The agents and their tickets are in agent/roles.py.

Start the control plane first, in a terminal of its own:

  .demo/bin/trustvian-local --state-dir .demo/live --listen 127.0.0.1:8080

then `TRUSTVIAN_API_URL=http://127.0.0.1:8080 make live`, which starts the
mock services and runs live.py with .demo/bin on PATH.

Ctrl-C (or SIGTERM) sends SIGINT to every dev, which stops its agent and
completes its run; whatever is left after LIVE_STOP_GRACE_SECONDS is killed,
dev, Collector and agent alike. live.py exits 0. A dev that exits early is
restarted after a back-off, as a new run; the others keep working.

  TRUSTVIAN_API_URL         required; the control plane's URL
  TRUSTVIAN_PROJECT         support-demo
  LIVE_AGENTS               support,billing,accounts
  LIVE_INTERVAL_SECONDS     10   one ticket per agent this often, jittered +-50%
  LIVE_DRIFT_EVERY          20   every Nth billing ticket also exports; 0 = never
  LIVE_MAX_TICKETS          0    tickets per agent, then stop; 0 = forever
  LIVE_STOP_GRACE_SECONDS   20   how long Ctrl-C waits for the runs to complete
  CRM_ERROR_RATE            read by mock_services/server.py, not here

Arguments after `--` replace the command dev runs (the tests use a stub):
  python live.py -- python tests/live_stub_worker.py
"""
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROLES = ("support", "billing", "accounts")
PROJECT = "support-demo"
BIN = os.path.dirname(sys.executable)
WORKER = [os.path.join(BIN, "opentelemetry-instrument"), sys.executable, "harness/run_agent.py"]
START_CONTROL_PLANE = ".demo/bin/trustvian-local --state-dir .demo/live --listen 127.0.0.1:8080"
BACKOFF_SECONDS = (2, 4, 8, 16, 30)
# The next dev starts once the previous one is running its agent: two
# provisioning the same project at once can collide on creating it, and dev
# writes its Collector's configuration to one file per directory.
STAGGER_TIMEOUT_SECONDS = 30
RUN_LINE = re.compile(r"^\s+Run\s+(\S+)\s*$")
RUNNING_LINE = re.compile(r"^Running ")
COMPLETE_LINE = re.compile(r"^Run (\S+) complete\.")


class Agent:
    def __init__(self, role: str, command: list, max_tickets: int):
        self.role = role
        self.actor = f"{role}-agent"
        self.command = command
        self.max_tickets = max_tickets
        self.handled = 0  # tickets reported, across restarts
        self.crashes = 0  # consecutive early exits without a ticket in between
        self.restarts = 0
        self.proc = None
        self.reader = None
        self.running_agent = threading.Event()
        self.restart_at = None
        self.done = False

    def dev(self, api_url: str, project: str) -> list:
        return ["trustvian", "dev", "--api-url", api_url, "--project", project,
                "--agent", self.actor, "--candidate", f"live-{self.role}",
                "--instrumentation", "existing", "--", *self.command]

    def start(self, api_url: str, project: str, lines: queue.Queue) -> None:
        env = dict(os.environ)
        env["LIVE_ROLE"] = self.role
        # A restarted agent carries on numbering, and stops at the same total.
        env["LIVE_FIRST_TICKET"] = str(self.handled + 1)
        if self.max_tickets > 0:
            env["LIVE_MAX_TICKETS"] = str(self.max_tickets - self.handled)
        self.running_agent.clear()
        self.proc = subprocess.Popen(
            self.dev(api_url, project),
            cwd=HERE,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            # Not the terminal: dev hands a terminal on its stdin to the
            # workload, and three of them would fight over it.
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            # Its own session: Ctrl-C reaches live.py only, which then stops
            # each dev, and each dev its own agent.
            start_new_session=True,
        )
        self.reader = threading.Thread(target=self._read, args=(self.proc, lines), daemon=True)
        self.reader.start()
        self.restart_at = None

    def _read(self, proc, lines: queue.Queue) -> None:
        # Kept: the per-ticket lines, the run id from dev's banner, the run's
        # completion and dev's own errors. Everything else (the rest of the
        # banner, framework logs, the agent's answers) is dropped: it may
        # quote a ticket.
        for line in proc.stdout:
            if line.startswith("@live "):
                lines.put((self, "ticket", line))
            elif match := RUN_LINE.match(line):
                lines.put((self, "run", match[1]))
            elif RUNNING_LINE.match(line):
                self.running_agent.set()
            elif match := COMPLETE_LINE.match(line):
                lines.put((self, "complete", match[1]))
            elif line.startswith("trustvian dev: "):
                lines.put((self, "dev", line[len("trustvian dev: "):].strip()))
        proc.stdout.close()
        self.running_agent.set()

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


def parse(line: str) -> dict:
    fields = dict(part.split("=", 1) for part in line.split()[1:] if "=" in part)
    tools = fields.get("tools", "-")
    fields["tools"] = [] if tools == "-" else tools.split(",")
    return fields


def show(agent: Agent, kind: str, value: str) -> None:
    if kind == "ticket":
        agent.handled += 1
        agent.crashes = 0
        fields = parse(value)
        tools = ", ".join(fields["tools"]) or "none"
        line = (f"{agent.actor:<15} ticket {fields.get('ticket', '?'):<4} tools: {tools}"
                f"  ({fields.get('seconds', '?')} s)")
        if fields.get("failed", "0") != "0":
            line += f"  failed calls: {fields['failed']}"
        if fields.get("drift") == "1":
            line += "  [drift]"
        if fields.get("error"):
            line += f"  error: {fields['error']}"
    elif kind == "run":
        line = f"{agent.actor:<15} run {value}"
    elif kind == "complete":
        line = f"{agent.actor:<15} run {value} complete"
    else:
        line = f"{agent.actor:<15} dev: {value}"
    print(line, flush=True)


def main(argv: list) -> int:
    command = argv[argv.index("--") + 1:] if "--" in argv else WORKER
    api_url = os.environ.get("TRUSTVIAN_API_URL", "").strip()
    if not api_url:
        print(f"live: set TRUSTVIAN_API_URL to a running control plane; start one with `{START_CONTROL_PLANE}`",
              file=sys.stderr)
        return 2
    project = os.environ.get("TRUSTVIAN_PROJECT") or PROJECT
    roles = [r.strip() for r in os.environ.get("LIVE_AGENTS", ",".join(ROLES)).split(",") if r.strip()]
    unknown = [r for r in roles if r not in ROLES]
    if not roles or unknown:
        print(f"live: LIVE_AGENTS must name some of {', '.join(ROLES)}; got {', '.join(unknown) or 'none'}",
              file=sys.stderr)
        return 2
    max_tickets = int(os.environ.get("LIVE_MAX_TICKETS", "0"))
    interval = os.environ.get("LIVE_INTERVAL_SECONDS", "10")
    grace = float(os.environ.get("LIVE_STOP_GRACE_SECONDS", "20"))

    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())

    lines = queue.Queue()
    agents = [Agent(role, command, max_tickets) for role in roles]
    print(f"live: {len(agents)} agents ({', '.join(a.actor for a in agents)}), one run each on {api_url}, "
          f"one ticket each every ~{interval} s"
          + (f", {max_tickets} each" if max_tickets > 0 else ", until stopped (Ctrl-C)"), flush=True)
    print(f"live: watch at {os.environ.get('TRUSTVIAN_WEB_URL') or api_url.rstrip('/') + '/'}", flush=True)

    def drain(timeout: float) -> bool:
        try:
            show(*lines.get(timeout=timeout))
            return True
        except queue.Empty:
            return False

    for agent in agents:
        if stopping.is_set():
            break
        agent.start(api_url, project, lines)
        deadline = time.monotonic() + STAGGER_TIMEOUT_SECONDS
        while not agent.running_agent.is_set() and not stopping.is_set() and time.monotonic() < deadline:
            drain(0.1)

    while not stopping.is_set() and not all(a.done for a in agents):
        if drain(0.2):
            continue
        now = time.monotonic()
        for agent in agents:
            if agent.done or agent.proc is None:
                continue
            if agent.restart_at is not None:
                if now >= agent.restart_at:
                    agent.restarts += 1
                    print(f"live: restarting {agent.actor} (restart {agent.restarts})", flush=True)
                    agent.start(api_url, project, lines)
                continue
            status = agent.proc.poll()
            if status is None:
                continue
            agent.reader.join(timeout=1)  # its last lines are queued before it counts as gone
            if not lines.empty():
                continue  # count them first, then decide
            if max_tickets > 0 and agent.handled >= max_tickets:
                agent.done = True
                continue
            delay = BACKOFF_SECONDS[min(agent.crashes, len(BACKOFF_SECONDS) - 1)]
            agent.crashes += 1
            agent.restart_at = now + delay
            print(f"live: {agent.actor}'s dev exited with status {status}; restarting in {delay} s",
                  flush=True)

    if stopping.is_set():
        print("live: stopping; each dev completes its run", flush=True)
    stop(agents, grace)
    while drain(0):
        pass
    print("live: " + ", ".join(f"{a.actor} {a.handled} tickets" for a in agents), flush=True)
    return 0


def stop(agents: list, grace: float) -> None:
    """SIGINT every dev, as Ctrl-C would; SIGKILL whatever is left after the grace period."""
    for agent in agents:
        if agent.running():
            try:
                os.kill(agent.proc.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + grace
    for agent in agents:
        if agent.proc is None:
            continue
        try:
            agent.proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            # dev runs the Collector and the agent each in a process group of
            # its own, so the whole tree is found before anything is killed.
            print(f"live: {agent.actor}'s dev did not finish within {grace:g} s; killing it", flush=True)
            kill_tree(agent.proc.pid)
            agent.proc.wait()
        if agent.reader:
            agent.reader.join(timeout=1)


def kill_tree(pid: int) -> None:
    own = os.getpgrp()
    for member in tree(pid):
        try:
            group = os.getpgid(member)
            if group != own:
                os.killpg(group, signal.SIGKILL)
            os.kill(member, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def tree(pid: int) -> list:
    """pid and every process below it."""
    try:
        listing = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True,
                                 timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return [pid]
    children = {}
    for row in listing.splitlines():
        parts = row.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    found, todo = [], [pid]
    while todo:
        current = todo.pop()
        found.append(current)
        todo.extend(children.get(current, []))
    return found


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

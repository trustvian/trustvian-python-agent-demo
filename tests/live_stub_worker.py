"""A stand-in for the instrumented agent in live mode: roles.serve() with no model.

Each ticket "calls" the tools the ticket binds. Driven by the environment:
  STUB_DIR             writes <role>.pid and <role>.service there
  STUB_CRASH_AT        exits 1 when this ticket number is reached, once
  STUB_GRANDCHILD      starts a long `sleep` first, to prove it is cleaned up
  STUB_IGNORE_TERM     ignores SIGTERM, so only SIGKILL stops it
"""
import os
import signal
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent import roles  # noqa: E402

CALLS = []
role = os.environ["LIVE_ROLE"]
stub_dir = os.environ["STUB_DIR"]


def record(name: str, value: str) -> None:
    with open(os.path.join(stub_dir, f"{role}.{name}"), "a") as f:
        f.write(value + "\n")


record("pid", str(os.getpid()))
record("service", os.environ.get("OTEL_SERVICE_NAME", ""))
if os.environ.get("STUB_GRANDCHILD"):
    record("pid", str(subprocess.Popen(["sleep", "300"]).pid))


def handle(ticket: dict) -> None:
    if os.environ.get("STUB_IGNORE_TERM"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    crash_at = os.environ.get("STUB_CRASH_AT")
    marker = os.path.join(stub_dir, f"{role}.crashed")
    if crash_at and ticket["number"] == int(crash_at) and not os.path.exists(marker):
        open(marker, "w").close()
        os._exit(1)
    for name in ticket["tools"]:
        CALLS.append((name, True))


roles.serve(handle, CALLS)

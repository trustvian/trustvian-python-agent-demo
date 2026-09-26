"""The local world a scenario's workload talks to, and the runtime above it.

Three things, each with a readiness condition rather than a sleep: the mock
backend services, the Trustvian control plane, and — for scenarios that need
one — a local Ollama.
"""

from __future__ import annotations

import contextlib
import json
import socket
import subprocess
import time
import urllib.error
import urllib.request

OLLAMA_API = "http://127.0.0.1:11434"


class WorldError(Exception):
    """Something the scenario needs is not there. Operational, exit 3."""


def _free_port() -> int:
    with contextlib.closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _get(url, timeout=2):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read()


def _wait_for(url, what, process=None, timeout=30):
    """Poll until the endpoint answers, or the process it belongs to dies.

    Noticing the process die is the difference between reporting the cause and
    reporting a symptom thirty seconds later.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            _get(url)
            return
        except (urllib.error.URLError, OSError):
            pass
        if process is not None and process.poll() is not None:
            raise WorldError(f"{what} exited during startup (status {process.returncode})")
        time.sleep(0.1)
    raise WorldError(f"timed out after {timeout}s waiting for {what} at {url}")


class Mocks:
    """mock_services/server.py, on a port the OS chose."""

    def __init__(self, root, python):
        self.root = root
        self.python = str(python)
        self.port = None
        self._process = None

    def __enter__(self):
        self.port = _free_port()
        log = open(self.root / ".runtime" / "mocks.log", "w")
        self._log = log
        self._process = subprocess.Popen(
            [self.python, str(self.root / "mock_services" / "server.py"),
             "--port", str(self.port)],
            stdout=log, stderr=subprocess.STDOUT)
        _wait_for(f"http://127.0.0.1:{self.port}/healthz", "the mock services",
                  self._process)
        return self

    def __exit__(self, *_):
        if self._process is not None:
            self._process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self._process.wait(timeout=10)
        self._log.close()
        return False


class Runtime:
    """The Trustvian control plane, started through scripts/tv-dev.sh.

    Started once and attached to by every run, rather than once per run:
    `runs: N` means N invocations of the wrapper, and N control planes would
    be N databases with nothing to compare across.
    """

    def __init__(self, root, api_url=None):
        self.root = root
        self.api_url = api_url
        self._started = False

    def __enter__(self):
        if self.api_url:
            return self
        completed = subprocess.run(
            [str(self.root / "scripts" / "tv-dev.sh"), "runtime", "up"],
            capture_output=True, text=True)
        if completed.returncode != 0:
            raise WorldError(
                f"could not start the Trustvian runtime: {completed.stderr.strip()}")
        self.api_url = completed.stdout.strip().splitlines()[-1]
        self._started = True
        return self

    def __exit__(self, *_):
        if self._started:
            subprocess.run([str(self.root / "scripts" / "tv-dev.sh"), "runtime", "down"],
                           capture_output=True, text=True)
        return False


def require_ollama(model):
    """Check a local Ollama is serving the model, without starting one.

    Deliberately does not start a server. `make demo` does that through
    scripts/lib.sh, which is careful to stop only a server it started itself —
    a developer's own Ollama, serving other work, must survive this
    repository. Duplicating that care here to save one command would be the
    wrong trade.
    """
    try:
        _get(f"{OLLAMA_API}/api/version")
    except (urllib.error.URLError, OSError):
        raise WorldError(
            f"this scenario needs a local Ollama at {OLLAMA_API}, and nothing "
            f"is answering there.\n"
            f"       Start one with `ollama serve`, then re-run.") from None

    try:
        tags = json.loads(_get(f"{OLLAMA_API}/api/tags", timeout=10))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise WorldError(f"could not list the models Ollama has: {exc}") from None

    installed = {entry.get("name", "") for entry in tags.get("models", [])}
    if model not in installed:
        raise WorldError(
            f"this scenario needs the model {model!r}, which Ollama does not "
            f"have.\n       Install it with: ollama pull {model}")

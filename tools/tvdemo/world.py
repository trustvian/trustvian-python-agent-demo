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

# The address **the agent itself calls**, from agent/planner.py's own constants.
#
# Not imported from there: that module is the application under test and pulls in
# `requests`, which this repository's tooling virtualenv deliberately does not
# have. tests/test_demo_contract.py asserts the two agree, the same way the model
# name is already pinned.
AGENT_OLLAMA_HOST = "ollama.localhost"
AGENT_OLLAMA_PORT = 11434


class WorldError(Exception):
    """Something the scenario needs is not there. Operational, exit 3."""


def _free_port() -> int:
    with contextlib.closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _port_is_listening(host, port, timeout=2):
    """Whether anything accepts a connection there.

    Weaker than an HTTP probe on purpose: its only job is to tell an absent
    server apart from a silent one.
    """
    with contextlib.closing(socket.socket()) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


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
    """The Trustvian control plane, started through scripts/runtime.sh.

    Started once and attached to by every run, rather than once per run:
    `runs: N` means N invocations of `trustvian dev`, and N control planes would
    be N databases with nothing to compare across. dev starts one of its own
    when no --api-url is given and stops it on the way out, which is right for
    one run and wrong for several.
    """

    def __init__(self, root, api_url=None):
        self.root = root
        self.api_url = api_url
        self._started = False

    def __enter__(self):
        if self.api_url:
            return self
        completed = subprocess.run(
            [str(self.root / "scripts" / "runtime.sh"), "up"],
            capture_output=True, text=True)
        if completed.returncode != 0:
            raise WorldError(
                f"could not start the Trustvian runtime: {completed.stderr.strip()}")
        self.api_url = completed.stdout.strip().splitlines()[-1]
        self._started = True
        return self

    def __exit__(self, *_):
        if self._started:
            subprocess.run([str(self.root / "scripts" / "runtime.sh"), "down"],
                           capture_output=True, text=True)
        return False


def require_ollama(model, timeout=180):
    """Require that the model answers, on the address the agent will use.

    Deliberately does not start a server. `make demo` does that through
    scripts/lib.sh, which is careful to stop only a server it started itself — a
    developer's own Ollama, serving other work, must survive this repository.
    Duplicating that care here to save one command would be the wrong trade.

    What it does check is stronger than what it used to. The old version asked
    ``GET /api/version`` on 127.0.0.1 and then listed models. Both pass in
    situations where the agent cannot work:

    * **127.0.0.1 is not the address the agent calls.** ``agent/planner.py`` calls
      ``ollama.localhost``, which on macOS resolves to ``::1`` first while Ollama
      binds IPv4 only. Today the client retries the next address; where ``::1`` is
      filtered rather than refused, every model call hangs while a 127.0.0.1 probe
      stays green.
    * **A stopped server passes a connect check.** Measured: a suspended
      ``ollama serve`` keeps its listening socket, so the kernel completes the
      handshake and nothing ever answers.
    * **``/api/tags`` reads metadata from disk.** A model whose weights are corrupt
      or too large for available memory is listed, then fails on first use.

    So this makes one real generation through the agent's own URL. Nothing of the
    reply is returned or logged: it is a model completion, and invariant 7 of the
    design doc admits no exemption for a readiness probe.
    """
    url = f"http://{AGENT_OLLAMA_HOST}:{AGENT_OLLAMA_PORT}"

    # Distinguish "nothing is there" from "something is there and silent". They
    # are different problems with different fixes, and reporting the second as the
    # first sends a developer looking for a port conflict they do not have.
    if not _port_is_listening(AGENT_OLLAMA_HOST, AGENT_OLLAMA_PORT):
        raise WorldError(
            f"this scenario needs a local Ollama reachable at {url}, and nothing "
            f"is listening there.\n"
            f"       Start one with `ollama serve`, then re-run.")

    payload = json.dumps({
        "model": model,
        "stream": False,
        "messages": [{"role": "user", "content": "Reply with: ok"}],
        # The content is irrelevant; that a generation completes is the point.
        "options": {"temperature": 0, "num_predict": 4},
    }).encode()

    request = urllib.request.Request(
        f"{url}/api/chat", data=payload,
        headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            answer = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        # Ollama's own error envelope is far more useful than the status code.
        detail = exc.read()[:400].decode("utf-8", "replace")
        raise WorldError(
            f"{url} refused to generate with model {model!r}: {detail}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise WorldError(
            f"the model did not answer at {url} within {timeout}s: {exc}\n"
            f"       That address is the one agent/planner.py calls, checked here "
            f"rather than 127.0.0.1 on purpose.\n"
            f"       A suspended server produces exactly this — check:\n"
            f"           ps -o pid,stat,command= -p $(pgrep -f 'ollama serve')\n"
            f"       A STAT of 'T' means stopped: it accepts connections and "
            f"never answers.") from None

    if not (answer.get("message") or {}).get("content"):
        raise WorldError(
            f"{url} answered for model {model!r}, but with no usable reply. The "
            f"agent's first turn would fail the same way. Ollama said: "
            f"{json.dumps(answer)[:400]}")

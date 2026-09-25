"""Contract tests for the two entry points.

These read the shell scripts rather than running them. The properties they
pin are ones a passing `make smoke` cannot demonstrate on its own: that the
deterministic path never waits for a human, and that the interactive path is
wired to the model this demo is documented around. Both are the kind of thing
that breaks silently — a prompt added to a shared function hangs CI, and a
changed default model makes every documented figure wrong — so they are
asserted here, where CI sees them.

No test in this file runs a model, a server or a shell script.
"""

import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "lib.sh"
DEMO = ROOT / "scripts" / "demo.sh"
SMOKE = ROOT / "scripts" / "smoke.sh"


def body_of(path: Path) -> str:
    """The script's source with whole-line comments removed.

    A comment mentioning `pause` is documentation, not a call, and a test that
    cannot tell those apart would block people from explaining the code.
    """
    return "\n".join(
        line for line in path.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )


class NonInteractiveSmokeTest(unittest.TestCase):
    """`make smoke` is what CI runs. It must never wait for a human."""

    def test_smoke_never_pauses(self):
        self.assertNotIn("pause ", body_of(SMOKE))

    def test_smoke_never_reads_stdin(self):
        body = body_of(SMOKE)
        for form in ("read -r", "read -p", "$(read"):
            self.assertNotIn(form, body, f"smoke.sh must not block on {form!r}")

    def test_the_demo_is_the_one_that_pauses(self):
        # The counterpart to the assertions above: if `pause` ever stopped
        # being called at all, they would pass for the wrong reason.
        self.assertIn("pause ", body_of(DEMO))

    def test_pause_does_not_wait_without_a_terminal(self):
        # An unattended caller must not hang. The guard is what makes the same
        # script usable interactively and from a script.
        lib = LIB.read_text()
        start = lib.index("pause() {")
        end = lib.index("\n}", start)
        self.assertIn("[ -t 0 ]", lib[start:end])

    def test_smoke_is_syntactically_valid_under_bash_3_2(self):
        # /bin/bash on macOS is 3.2.57, and CI runs a modern bash; a construct
        # only one accepts would break exactly one of them.
        for shell in ("/bin/bash", "bash"):
            with self.subTest(shell=shell):
                proc = subprocess.run([shell, "-n", str(SMOKE)],
                                      capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)


class DocumentedModelTest(unittest.TestCase):
    """The demo is documented around one model. Nothing may quietly swap it."""

    def test_lib_defaults_to_gemma3_4b(self):
        self.assertIn('OLLAMA_MODEL_NAME="${OLLAMA_MODEL:-gemma3:4b}"',
                      LIB.read_text())

    def test_planner_defaults_to_gemma3_4b(self):
        from agent import planner
        self.assertEqual(planner.DEFAULT_MODEL, "gemma3:4b")

    def test_the_agent_calls_a_local_ollama_chat_endpoint(self):
        from agent import planner
        url = planner.default_url()
        self.assertTrue(url.endswith("/api/chat"), url)
        self.assertIn("ollama", url)
        self.assertIn("11434", url)

    def test_no_cloud_inference_credentials_anywhere_in_the_agent(self):
        # The inference path is local. Package installation still uses PyPI,
        # which is a different thing and stays.
        for name in ("main", "planner", "tools"):
            source = (ROOT / "agent" / f"{name}.py").read_text()
            for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
                        "AZURE_OPENAI", "BEDROCK"):
                self.assertNotIn(key, source, f"{name}.py references {key}")


class ApplicationDependencyTest(unittest.TestCase):
    """What the application declares it needs, and what it must not."""

    def declared(self):
        lines = (ROOT / "agent" / "requirements.txt").read_text().splitlines()
        return [ln.strip() for ln in lines
                if ln.strip() and not ln.lstrip().startswith("#")]

    def test_requests_is_the_only_declared_dependency(self):
        self.assertEqual(self.declared(), ["requests==2.32.3"])

    def test_no_agent_framework_was_added(self):
        # A framework would obscure the telemetry this demo exists to show,
        # and the loop is small enough to read without one.
        declared = " ".join(self.declared()).lower()
        for framework in ("langchain", "langgraph", "crewai", "autogen",
                          "semantic-kernel", "llama-index"):
            self.assertNotIn(framework, declared)

    def test_no_observability_or_engine_dependency_is_declared(self):
        declared = " ".join(self.declared()).lower()
        for forbidden in ("trustvian", "opentelemetry"):
            self.assertNotIn(forbidden, declared)


class EvaluationPhaseTest(unittest.TestCase):
    """The interactive demo needs the lifecycle in separable phases."""

    def test_lib_exposes_the_three_phases(self):
        lib = LIB.read_text()
        for fn in ("begin_evaluation()", "observe_agent()", "end_evaluation()"):
            self.assertIn(fn, lib)

    def test_run_evaluation_still_composes_them(self):
        # smoke.sh calls run_evaluation, so the one-call form must survive the
        # split rather than being replaced by it.
        lib = LIB.read_text()
        start = lib.index("run_evaluation() {")
        body = lib[start:lib.index("\n}", start)]
        for fn in ("begin_evaluation", "observe_agent", "end_evaluation"):
            self.assertIn(fn, body)

    def test_the_live_view_capability_is_probed_over_http(self):
        # Asked of the running server, not grepped out of a checkout: the
        # route either answers or it does not.
        lib = LIB.read_text()
        start = lib.index("live_view_available() {")
        body = lib[start:lib.index("\n}", start)]
        self.assertIn("/v1/projects", body)
        self.assertIn("curl", body)


if __name__ == "__main__":
    unittest.main()

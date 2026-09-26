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

import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "lib.sh"
DEMO = ROOT / "scripts" / "demo.sh"
SMOKE = ROOT / "scripts" / "smoke.sh"
TVDEV = ROOT / "scripts" / "tv-dev.sh"


def body_of(path: Path) -> str:
    """The script's source with whole-line comments removed.

    A comment mentioning `pause` is documentation, not a call, and a test that
    cannot tell those apart would block people from explaining the code.
    """
    return "\n".join(
        line for line in path.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )


def function_body(path: Path, name: str) -> str:
    """One shell function's body, comments and all."""
    source = path.read_text()
    start = source.index(f"{name}() {{")
    return source[start:source.index("\n}", start)]


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


class WrapperContractTest(unittest.TestCase):
    """Both entry points go through one wrapper, shaped like task 077."""

    def test_the_wrapper_exists_and_is_executable(self):
        self.assertTrue(TVDEV.exists(), "scripts/tv-dev.sh is missing")
        self.assertTrue(os.access(TVDEV, os.X_OK), "scripts/tv-dev.sh is not executable")

    def test_the_wrapper_is_valid_under_bash_3_2(self):
        # /bin/bash on macOS is 3.2.57, and CI runs a modern bash; a construct
        # only one accepts would break exactly one of them.
        for shell in ("/bin/bash", "bash"):
            with self.subTest(shell=shell):
                proc = subprocess.run([shell, "-n", str(TVDEV)],
                                      capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_both_entry_points_use_it(self):
        # A smoke test that drove a private code path would be asserting
        # something nobody runs.
        for script in (DEMO, SMOKE):
            with self.subTest(script=script.name):
                self.assertIn("tv-dev.sh", body_of(script))

    def test_the_wrapper_takes_its_workload_after_a_double_dash(self):
        # The property that lets a developer point this at their own agent,
        # which is the whole reason the wrapper exists.
        body = body_of(TVDEV)
        self.assertIn('CHILD=("$@")', body)

    def test_the_wrapper_never_pauses(self):
        # It is called from CI, from the stability sweep and from the bench.
        body = body_of(TVDEV)
        self.assertNotIn("pause ", body)
        for form in ("read -r", "read -p"):
            self.assertNotIn(form, body, f"tv-dev.sh must not block on {form!r}")

    def test_a_failed_workload_fails_its_run_instead_of_completing_it(self):
        # A workload that crashed produced no verdict, not a passing one. The
        # exit-3-versus-1 distinction the CI gate depends on starts here.
        body = body_of(TVDEV)
        self.assertIn("eval fail", body)
        self.assertIn("CHILD_STATUS", body)

    def test_the_otel_environment_is_composed_in_exactly_one_place(self):
        # The application never sets these and never sees them in its
        # manifest; exactly one file supplies them, at launch.
        marker = "OTEL_EXPORTER_OTLP_ENDPOINT"
        self.assertIn(marker, TVDEV.read_text())
        for other in (LIB, DEMO, SMOKE):
            with self.subTest(script=other.name):
                self.assertNotIn(marker, other.read_text())

    def test_the_stable_semconv_opt_in_is_set(self):
        # Without it the instrumentation emits legacy http.url/http.method,
        # Trustvian's processor reads server.address, and every span arrives
        # with an empty target — collapsing the observed behaviors.
        self.assertIn("OTEL_SEMCONV_STABILITY_OPT_IN", TVDEV.read_text())


class CapabilityProbeTest(unittest.TestCase):
    """Every Trustvian capability is asked for at runtime, never assumed."""

    def test_the_live_view_capability_is_probed_over_http(self):
        # Asked of the running server, not grepped out of a checkout: the
        # route either answers or it does not.
        body = function_body(LIB, "live_view_available")
        self.assertIn("/v1/projects", body)
        self.assertIn("curl", body)

    def test_the_cli_capabilities_are_probed_by_asking_the_binary(self):
        for name in ("supports_environments", "trustvian_dev_available",
                     "scenario_runner_available"):
            with self.subTest(probe=name):
                body = function_body(LIB, name)
                self.assertIn("--help", body)
                self.assertIn("$BIN_DIR/trustvian", body)

    def test_semantic_fidelity_is_probed_from_a_control_plane_response(self):
        # Task 075's arrival is visible in what the server answers, not in
        # the source of the sibling checkout.
        body = function_body(LIB, "tool_fidelity_in")
        self.assertIn("operation_category", body)
        self.assertIn("behavior_diff", body)

    def test_no_probe_reads_the_trustvian_checkout(self):
        # bootstrap.sh legitimately inspects the checkout to decide whether it
        # can build at all. A *capability* probe must not: a source grep stops
        # being true the moment the file moves.
        lib = LIB.read_text()
        self.assertNotIn("TRUSTVIAN_DIR", lib)


class WrapperStandInTest(unittest.TestCase):
    """The wrapper is a stand-in, and says so where someone will see it."""

    def test_it_names_the_task_it_stands_in_for(self):
        self.assertIn("077", TVDEV.read_text())

    def test_it_reports_when_the_real_command_has_shipped(self):
        body = body_of(TVDEV)
        self.assertIn("trustvian_dev_available", body)


if __name__ == "__main__":
    unittest.main()

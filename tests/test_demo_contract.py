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
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "lib.sh"
DEMO = ROOT / "scripts" / "demo.sh"
SMOKE = ROOT / "scripts" / "smoke.sh"
RUNTIME = ROOT / "scripts" / "runtime.sh"
CLEAN = ROOT / "scripts" / "clean.sh"


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


class DevAdoptionTest(unittest.TestCase):
    """Every workload runs through `trustvian dev`, and nothing stands in."""

    def test_the_stand_in_is_gone(self):
        # Task 077 shipped. A fallback path would mean two orchestrations, and
        # the one a user trusts is whichever they happened to run.
        self.assertFalse(
            (ROOT / "scripts" / "tv-dev.sh").exists(),
            "scripts/tv-dev.sh is back; trustvian dev has shipped and there is "
            "no fallback path")

    def test_no_script_mentions_the_stand_in(self):
        for script in (LIB, DEMO, SMOKE, RUNTIME, CLEAN):
            with self.subTest(script=script.name):
                self.assertNotIn("tv-dev", script.read_text())

    def test_both_entry_points_run_workloads_through_dev(self):
        # A smoke test that drove a private code path would be asserting
        # something nobody runs.
        for script in (DEMO, SMOKE):
            with self.subTest(script=script.name):
                self.assertIn("dev_run ", body_of(script))

    def test_dev_run_invokes_the_real_command(self):
        body = function_body(LIB, "dev_run")
        self.assertIn('"$BIN_DIR/trustvian" dev', body)
        self.assertIn('-- "$VENV_DIR/bin/opentelemetry-instrument"', body)

    def test_dev_run_states_the_instrumentation_mode(self):
        # The workload runs through opentelemetry-instrument, which dev's `auto`
        # would accept as positive evidence. A demo should not depend on that
        # inference, so the mode is named.
        self.assertIn("--instrumentation existing", function_body(LIB, "dev_run"))

    def test_dev_run_names_every_identity_value(self):
        # Both sides of the comparison come from one commit, so dev's own
        # derivation would give them one candidate id — and therefore one
        # learning profile, because the candidate is the learning scope.
        body = function_body(LIB, "dev_run")
        for flag in ("--project", "--agent", "--environment", "--candidate",
                     "--run-id"):
            with self.subTest(flag=flag):
                self.assertIn(flag, body)

    def test_the_helper_binaries_are_exported_once(self):
        # dev resolves them flag -> environment -> beside the executable ->
        # PATH, and this repository builds them somewhere that is none of those.
        body = function_body(LIB, "demo_init")
        self.assertIn("export TRUSTVIAN_LOCAL_BIN=", body)
        self.assertIn("export TRUSTVIAN_COLLECTOR_BIN=", body)

    def test_a_build_without_dev_is_refused_rather_than_worked_around(self):
        body = function_body(LIB, "require_trustvian_dev")
        self.assertIn("--help", body)
        self.assertIn("$BIN_DIR/trustvian", body)
        self.assertIn("fail ", body)
        for script in (DEMO, SMOKE):
            with self.subTest(script=script.name):
                self.assertIn("require_trustvian_dev", body_of(script))

    def test_a_failed_workload_fails_its_run_instead_of_completing_it(self):
        # dev does this, and dev_run must not paper over it: a workload that
        # crashed produced no verdict, not a passing one. The exit-3-versus-1
        # distinction the CI gate depends on starts here.
        body = function_body(LIB, "dev_run")
        self.assertIn("failed, not completed", body)

    def test_no_script_composes_the_otel_environment_any_more(self):
        # dev owns it now. A script that also set these would be configuring
        # the thing it delegated, and the two could disagree.
        for script in (LIB, DEMO, SMOKE, RUNTIME, CLEAN):
            with self.subTest(script=script.name):
                self.assertNotIn("OTEL_EXPORTER_OTLP_ENDPOINT",
                                 script.read_text())

    def test_the_scripts_are_valid_under_bash_3_2(self):
        # /bin/bash on macOS is 3.2.57, and CI runs a modern bash; a construct
        # only one accepts would break exactly one of them.
        for script in (LIB, DEMO, SMOKE, RUNTIME, CLEAN):
            for shell in ("/bin/bash", "bash"):
                with self.subTest(script=script.name, shell=shell):
                    proc = subprocess.run([shell, "-n", str(script)],
                                          capture_output=True, text=True)
                    self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_clean_reports_the_dev_state_path_and_does_not_remove_it(self):
        """Run it, in a throwaway tree, and check the directory survives.

        Asserted by behavior rather than by reading the script, because the
        property is "this path is still there afterwards" and a grep for `rm -rf`
        cannot distinguish the command from the suggestion clean.sh prints.

        dev keys its state by a hash of the workload directory. Re-deriving that
        path here would be the second implementation whose failure mode is
        `rm -rf` on the wrong directory, so clean.sh only ever prints what dev
        reported — and this test is what holds that.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "repo"
            (tree / "scripts").mkdir(parents=True)
            (tree / "scripts" / "clean.sh").write_bytes(CLEAN.read_bytes())
            (tree / "scripts" / "clean.sh").chmod(0o755)
            for generated in (".demo", ".runtime", ".trustvian"):
                (tree / generated).mkdir()

            # Stand in for what a dev run would have recorded.
            dev_state = Path(tmp) / "pretend-dev-state"
            dev_state.mkdir()
            (dev_state / "baseline-reference.json").write_text("{}")
            (tree / ".runtime" / "dev-state-path").write_text(f"{dev_state}\n")

            proc = subprocess.run([str(tree / "scripts" / "clean.sh")],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)

            for generated in (".demo", ".runtime", ".trustvian"):
                with self.subTest(removed=generated):
                    self.assertFalse((tree / generated).exists(),
                                     f"{generated} should have been removed")

            self.assertTrue(dev_state.exists(),
                            "clean.sh removed trustvian dev's state directory, "
                            "which this repository did not derive")
            self.assertIn(str(dev_state), proc.stdout,
                          "clean.sh did not name the path dev reported")


class OllamaReadinessTest(unittest.TestCase):
    """The readiness gate must prove the model, at the agent's own address.

    Both halves are load-bearing and both used to be wrong:

    * the probe checked ``127.0.0.1`` while the agent calls ``ollama.localhost``,
      which resolves to ``::1`` first on macOS while Ollama binds IPv4 only;
    * the probe was ``GET /api/version`` plus ``ollama list``, neither of which
      loads the model or generates a token — and a *suspended* server passes a
      connect check, because it keeps its listening socket.
    """

    def test_the_orchestration_agrees_with_the_agent_on_the_address(self):
        """The one duplication, pinned rather than trusted.

        ``agent/planner.py`` cannot be imported by the tooling virtualenv (it
        pulls in ``requests``), so the host and port are written in three places.
        This is what makes that safe.
        """
        from agent import planner
        sys.path.insert(0, str(ROOT / "tools"))
        from tvdemo import world

        self.assertEqual(world.AGENT_OLLAMA_HOST, planner.OLLAMA_HOST)
        self.assertEqual(world.AGENT_OLLAMA_PORT, planner.OLLAMA_PORT)
        lib = LIB.read_text()
        self.assertIn(f'OLLAMA_HOST_FOR_AGENT="{planner.OLLAMA_HOST}"', lib)
        self.assertIn(f'OLLAMA_PORT_FOR_AGENT="{planner.OLLAMA_PORT}"', lib)

    def test_the_gate_probes_the_agents_url_not_the_loopback_one(self):
        """Asserted on the variable, not on the string "127.0.0.1".

        That string legitimately appears in the failure message, which explains
        why the gate does *not* use it — so a blunt search flags the
        documentation along with the defect. What must not appear is
        ``$OLLAMA_API``, the loopback-only endpoint: using it here is the actual
        mistake, and it is the only way this function could probe the wrong
        address.
        """
        body = function_body(LIB, "require_model_answers")
        self.assertIn("$OLLAMA_AGENT_API/api/chat", body)
        self.assertNotIn("$OLLAMA_API", body)

    def test_the_gate_makes_a_real_generation(self):
        # /api/version and /api/tags both answer without the model being loaded.
        body = function_body(LIB, "require_model_answers")
        self.assertIn("/api/chat", body)
        self.assertIn("$OLLAMA_MODEL_NAME", body)
        self.assertIn(".message.content", body)

    def test_the_gate_never_prints_the_model_reply(self):
        # It is a completion. Invariant 7 of the design doc admits no exemption
        # for a readiness probe, so only the fact and the duration are reported.
        body = function_body(LIB, "require_model_answers")
        self.assertNotIn('log "$reply', body)
        self.assertNotIn('echo "$reply', body)
        self.assertNotIn('printf .*$reply', body)

    def test_ensure_ollama_runs_the_gate(self):
        self.assertIn("require_model_answers", function_body(LIB, "ensure_ollama"))

    def test_a_silent_server_is_told_apart_from_an_absent_one(self):
        # Starting a second server against a held port reports "address already
        # in use", which describes the symptom and hides the cause.
        body = function_body(LIB, "ensure_ollama")
        self.assertIn("port_is_listening", body)
        self.assertIn("not\n       answering HTTP", body)

    def test_the_python_side_checks_the_same_way(self):
        sys.path.insert(0, str(ROOT / "tools"))
        source = (ROOT / "tools" / "tvdemo" / "world.py").read_text()
        self.assertIn("/api/chat", source)
        self.assertIn("_port_is_listening", source)
        # And it must not have kept the old loopback-only probe.
        self.assertNotIn('OLLAMA_API = "http://127.0.0.1:11434"', source)

    def test_the_agent_itself_gained_no_readiness_check(self):
        """The application under test stays free of scaffolding.

        The fix belongs in the orchestration. A readiness probe inside
        ``agent/planner.py`` would be test code in the application whose
        isolation this whole repository exists to demonstrate — and the agent
        failing on its first call when nobody answers is correct behavior.
        """
        source = (ROOT / "agent" / "planner.py").read_text()
        for smell in ("api/version", "api/tags", "require_", "readiness",
                      "is_up", "ping"):
            with self.subTest(pattern=smell):
                self.assertNotIn(smell, source)


class CapabilityProbeTest(unittest.TestCase):
    """Every Trustvian capability is asked for at runtime, never assumed."""

    def test_the_live_view_capability_is_probed_over_http(self):
        # Asked of the running server, not grepped out of a checkout: the
        # route either answers or it does not.
        body = function_body(LIB, "live_view_available")
        self.assertIn("/v1/projects", body)
        self.assertIn("curl", body)

    def test_the_cli_capabilities_are_probed_by_asking_the_binary(self):
        for name in ("supports_environments", "require_trustvian_dev",
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


if __name__ == "__main__":
    unittest.main()

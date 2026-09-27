"""What `trustvian dev` actually hands the workload.

Every other contract test in this repository reads a script. This one runs
`trustvian dev` for real, with a workload that does nothing but write its own
environment out, and reads that.

The difference matters. The two variables asserted below used to be set by this
repository's own wrapper, where a grep for the name was proof enough. They are
dev's now, and a grep of dev's source would be this repository asserting
something about a sibling checkout it does not own. What it can legitimately
require is that the environment arriving at its agent carries them — so that is
what is checked, at the only place it is observable.

Both are load-bearing and both fail silently:

    OTEL_SEMCONV_STABILITY_OPT_IN=http
        Without it the `requests` instrumentation emits the legacy
        http.url/http.method attributes while Trustvian's processor reads
        server.address and http.request.method, so every span arrives with an
        empty target and the observed behaviors collapse into fewer than there
        are. The comparison still succeeds. It is just wrong.

    deployment.environment.name=local
        The engine fills a record's environment from it, and the platform
        refuses a record whose environment differs from its run's. Without it a
        run collects zero usable evidence while every process reports success.

No model, no mock services, no agent: the workload is `env`. It needs a control
plane and a Collector, which is why this lives beside the scripts rather than in
the pure unit tests, and it skips when those have not been built.
"""

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / ".demo" / "bin"


def built() -> bool:
    return all((BIN / name).exists() for name in
               ("trustvian", "trustvian-local", "trustvian-collector"))


@unittest.skipUnless(built(), "run `make bootstrap` first: .demo/bin is empty")
class DevEnvironmentContractTest(unittest.TestCase):
    """The environment dev composes, read from the child that received it."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        dump = Path(cls._tmp.name) / "child-env"

        # `env` writes every variable it was given, one per line. A shell
        # wrapper only because the redirection target has to be a path this test
        # chose rather than wherever the command happened to run.
        workload = Path(cls._tmp.name) / "dump-env.sh"
        workload.write_text(f'#!/bin/sh\nenv > "{dump}"\n')
        workload.chmod(0o755)

        environment = dict(os.environ)
        environment["TRUSTVIAN_LOCAL_BIN"] = str(BIN / "trustvian-local")
        environment["TRUSTVIAN_COLLECTOR_BIN"] = str(BIN / "trustvian-collector")

        # dev starts its own control plane here — no --api-url — which is also
        # the only configuration in which the discovery fallback is reachable.
        # Its state lands under ~/.trustvian/dev/<hash of cwd>, never in this
        # repository, so the run id is unique per invocation to avoid colliding
        # with evidence a previous run of this test left in that database.
        run_id = f"env-contract-{os.getpid()}"
        completed = subprocess.run(
            [str(BIN / "trustvian"), "dev",
             "--project", "support-demo",
             "--agent", "support-agent",
             "--environment", "local",
             "--candidate", "env-contract",
             "--run-id", run_id,
             "--instrumentation", "existing",
             "--", str(workload)],
            cwd=str(ROOT), env=environment,
            capture_output=True, text=True, timeout=300)

        # The environment dev itself was handed, so the assertions below can tell
        # what dev added from what it merely passed through.
        cls.environment = environment
        cls.completed = completed
        cls.banner = completed.stdout
        cls.child_env = {}
        if dump.exists():
            for line in dump.read_text().splitlines():
                name, _, value = line.partition("=")
                cls.child_env[name] = value

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_dev_ran_the_workload(self):
        self.assertEqual(self.completed.returncode, 0,
                         f"stdout:\n{self.completed.stdout}\n"
                         f"stderr:\n{self.completed.stderr}")
        self.assertTrue(self.child_env,
                        "the workload wrote no environment, so nothing below is "
                        "asserting anything")

    def test_the_stable_semconv_opt_in_reaches_the_workload(self):
        value = self.child_env.get("OTEL_SEMCONV_STABILITY_OPT_IN", "")
        self.assertIn("http", [part.strip() for part in value.split(",")],
                      f"OTEL_SEMCONV_STABILITY_OPT_IN={value!r} does not opt in "
                      f"to the stable HTTP conventions; every span would arrive "
                      f"with an empty target")

    def test_the_deployment_environment_reaches_the_workload(self):
        attributes = self.child_env.get("OTEL_RESOURCE_ATTRIBUTES", "")
        declared = dict(
            part.split("=", 1) for part in attributes.split(",") if "=" in part)
        self.assertEqual(declared.get("deployment.environment.name"), "local",
                         f"OTEL_RESOURCE_ATTRIBUTES={attributes!r} does not "
                         f"declare deployment.environment.name=local; the "
                         f"platform would refuse every record")

    def test_the_service_name_agrees_with_the_agent_dev_provisioned(self):
        # The processor derives the actor from service.name. A run provisioned
        # for one agent whose telemetry declares another cannot be attributed.
        self.assertEqual(self.child_env.get("OTEL_SERVICE_NAME"), "support-agent")

    def test_the_exporter_points_at_loopback(self):
        endpoint = self.child_env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "")
        self.assertRegex(endpoint, r"^http://127\.0\.0\.1:\d+/v1/traces$",
                         "the workload's exporter is not aimed at a loopback "
                         "OTLP receiver")

    def test_dev_injects_no_instrumentation_of_its_own(self):
        """`existing` means dev configures OTLP and attaches nothing.

        Asserted as "dev did not change this", not as "the child does not have
        it". A developer's own shell legitimately exports some of these — this
        machine's editor sets PYTHONSTARTUP — and a test that read an inherited
        value as an injection would fail for the wrong reason on the wrong
        machines. What dev must not do is add one or alter one.
        """
        for variable in ("PYTHONSTARTUP", "PYTHONPATH", "LD_PRELOAD",
                         "DYLD_INSERT_LIBRARIES", "JAVA_TOOL_OPTIONS",
                         "_JAVA_OPTIONS", "NODE_OPTIONS"):
            with self.subTest(variable=variable):
                self.assertEqual(
                    self.child_env.get(variable), self.environment.get(variable),
                    f"dev changed {variable}, which is how a workload acquires a "
                    f"second instrumentation stack — and duplicate spans are a "
                    f"behavioral lie, not a degraded signal")

    def test_dev_adds_only_otel_and_trustvian_variables(self):
        """The whole delta, not a list of suspects.

        The test above names the variables a wrapper would inject. This one
        catches the ones nobody thought to name: every variable dev added or
        changed has to be an OpenTelemetry setting or a TRUSTVIAN_DEV_ one.
        """
        # SHLVL and _ are the shell's own bookkeeping, incremented and rewritten
        # by every subshell between this test and the workload. Neither is dev's,
        # and neither is something dev could avoid.
        SHELL_BOOKKEEPING = ("SHLVL", "_")

        changed = sorted(
            name for name, value in self.child_env.items()
            if self.environment.get(name) != value)
        unexpected = [name for name in changed
                      if not name.startswith(("OTEL_", "TRUSTVIAN_DEV_"))
                      and name not in SHELL_BOOKKEEPING]
        self.assertEqual(unexpected, [],
                         f"dev changed variables outside its own namespaces; "
                         f"the full delta was {changed}")

    def test_dev_keeps_its_state_outside_this_repository(self):
        # The banner's own State line, not a path this test derived.
        match = re.search(r"^  State\s+(\S+)", self.banner, re.MULTILINE)
        self.assertIsNotNone(match, f"no State line in dev's banner:\n{self.banner}")
        state = Path(match.group(1)).resolve()
        self.assertNotIn(ROOT.resolve(), [state, *state.parents],
                         f"dev put its state inside this repository at {state}")


if __name__ == "__main__":
    unittest.main()

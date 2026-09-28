#!/usr/bin/env python3
"""The harness wrapper is a strict pass-through, proven without a model.

Trustvian's task 080 requires exactly this shape: a fixture set including a
success, an unknown tool and an argument-validation failure, run against the
wrapped dispatch function, asserting arguments, return value, exceptions by name,
call count and spans.

Why it cannot be proven by comparing model runs: a wrapper that dropped every
third call would still produce two similar-looking behavior sets, and a sweep
would report a plausible number about a broken wrapper. The property is
deterministic, so the test is deterministic. This runs in CI with no model.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from agent.tools import ToolError
from harness import run_agent


class Recorder:
    """A stand-in dispatch that records exactly how it was called.

    Not a mock library: the whole assertion is about argument identity and call
    count, and a hand-written recorder makes what is asserted obvious.
    """

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, session, port, name, action, mode):
        self.calls.append((session, port, name, action, mode))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class WrapperPassThrough(unittest.TestCase):
    def setUp(self):
        # A fresh provider per test, so span assertions cannot see another
        # test's spans. The exporter is in-memory: nothing leaves the process.
        self.exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.provider = provider
        # The module-level tracer is captured at import time, so the test
        # replaces the tracer rather than the global provider.
        self._saved_tracer = run_agent._tracer
        run_agent._tracer = provider.get_tracer("test")

    def tearDown(self):
        run_agent._tracer = self._saved_tracer
        self.provider.shutdown()

    def spans(self):
        return list(self.exporter.get_finished_spans())

    # -----------------------------------------------------------------
    # Arguments, return value, call count
    # -----------------------------------------------------------------

    def test_arguments_reach_the_wrapped_function_unchanged(self):
        recorder = Recorder(["ok"])
        wrapped = run_agent.instrument_dispatch(recorder)

        session = object()
        action = {"action": "crm_lookup", "customer_id": "42"}
        wrapped(session, "8080", "crm_lookup", action, "reference")

        self.assertEqual(len(recorder.calls), 1)
        got_session, got_port, got_name, got_action, got_mode = recorder.calls[0]
        # `is` for the session and the action dict: the wrapper must not copy,
        # normalise or re-key what it passes through. A copy would be a
        # behavioural change the agent cannot see but a stateful tool could.
        self.assertIs(got_session, session)
        self.assertIs(got_action, action)
        self.assertEqual(got_port, "8080")
        self.assertEqual(got_name, "crm_lookup")
        self.assertEqual(got_mode, "reference")

    def test_return_value_is_returned_unchanged(self):
        sentinel = "customer 42 is Ada Lovelace"
        wrapped = run_agent.instrument_dispatch(Recorder([sentinel]))
        self.assertIs(
            wrapped(object(), "8080", "crm_lookup", {}, "reference"), sentinel)

    def test_one_call_in_is_one_call_out(self):
        """Never retried, reordered, batched, cached or suppressed."""
        recorder = Recorder(["a", "b", "c"])
        wrapped = run_agent.instrument_dispatch(recorder)

        for name in ("crm_lookup", "knowledge_search", "crm_lookup"):
            wrapped(object(), "8080", name, {}, "reference")

        self.assertEqual([c[2] for c in recorder.calls],
                         ["crm_lookup", "knowledge_search", "crm_lookup"],
                         "order or count changed")
        # The repeat is the cache probe: an identical second call to the same
        # tool must reach the dispatcher again.
        self.assertEqual(len(recorder.calls), 3)
        self.assertEqual(len(self.spans()), 3, "one span per dispatch")

    # -----------------------------------------------------------------
    # Exceptions — the three fixtures 080 names
    # -----------------------------------------------------------------

    def test_a_tool_error_propagates_with_its_payload(self):
        original = ToolError("'nope' is not available in reference mode")
        wrapped = run_agent.instrument_dispatch(Recorder([original]))

        with self.assertRaises(ToolError) as caught:
            wrapped(object(), "8080", "nope", {}, "reference")
        self.assertIs(caught.exception, original,
                      "the wrapper replaced the exception object")

    def test_an_argument_validation_failure_propagates(self):
        original = ToolError("customer_id 'bad/id' is not a valid identifier")
        wrapped = run_agent.instrument_dispatch(Recorder([original]))

        with self.assertRaises(ToolError) as caught:
            wrapped(object(), "8080", "crm_lookup", {"customer_id": "bad/id"},
                    "reference")
        self.assertEqual(str(caught.exception), str(original))

    def test_an_unexpected_exception_type_is_not_converted(self):
        """A transport failure must stay a transport failure.

        agent.tools deliberately does not catch these: the service is not there,
        and nothing the model chooses will change that. A wrapper that wrapped
        them in ToolError would make a dead service look like a bad tool choice.
        """
        wrapped = run_agent.instrument_dispatch(Recorder([RuntimeError("boom")]))
        with self.assertRaises(RuntimeError):
            wrapped(object(), "8080", "crm_lookup", {}, "reference")

    def test_a_failed_dispatch_still_emits_exactly_one_span(self):
        wrapped = run_agent.instrument_dispatch(Recorder([ToolError("no")]))
        with self.assertRaises(ToolError):
            wrapped(object(), "8080", "crm_lookup", {}, "reference")
        self.assertEqual(len(self.spans()), 1)

    # -----------------------------------------------------------------
    # Spans: the two attributes, and nothing else
    # -----------------------------------------------------------------

    def test_the_span_carries_the_operation_and_the_tool_name(self):
        wrapped = run_agent.instrument_dispatch(Recorder(["ok"]))
        wrapped(object(), "8080", "export_customer", {}, "candidate")

        spans = self.spans()
        self.assertEqual(len(spans), 1)
        attributes = dict(spans[0].attributes)
        self.assertEqual(attributes.get("gen_ai.operation.name"), "execute_tool")
        self.assertEqual(attributes.get("gen_ai.tool.name"), "export_customer")

    def test_the_span_carries_nothing_else_at_all(self):
        """The attribute set is exactly two keys.

        Asserted by naming the whole set rather than by checking a count, so a
        future addition fails with the key it added rather than with an
        off-by-one nobody can read.
        """
        wrapped = run_agent.instrument_dispatch(Recorder(["ok"]))
        wrapped(object(), "8080", "crm_lookup",
                {"customer_id": "42", "email_body": "Dear Ada"}, "reference")

        attributes = dict(self.spans()[0].attributes)
        self.assertEqual(
            set(attributes),
            {"gen_ai.operation.name", "gen_ai.tool.name"},
            f"the span gained attributes: {sorted(attributes)}")

    def test_no_argument_value_reaches_the_span(self):
        """Content-free, proven with a canary rather than by inspection.

        The values planted here are the kinds of thing a tool argument carries —
        a customer id and an email body. Neither may appear anywhere in the span,
        including in its name or its events.
        """
        wrapped = run_agent.instrument_dispatch(Recorder(["ok"]))
        wrapped(object(), "8080", "send_email", {
            "customer_id": "CANARY-CUSTOMER",
            "email_subject": "CANARY-SUBJECT",
            "email_body": "CANARY-BODY",
            "query": "CANARY-QUERY",
        }, "reference")

        span = self.spans()[0]
        haystack = "\n".join([
            span.name,
            repr(dict(span.attributes)),
            repr([(e.name, dict(e.attributes or {})) for e in span.events]),
        ])
        for canary in ("CANARY-CUSTOMER", "CANARY-SUBJECT", "CANARY-BODY",
                       "CANARY-QUERY"):
            self.assertNotIn(canary, haystack,
                             f"{canary} reached the span:\n{haystack}")

    def test_an_exception_message_does_not_reach_the_span(self):
        """record_exception=False, proven rather than trusted.

        This is the trap the default sets: str(exc) becomes a span event, and a
        ToolError quotes the model's own argument. The canary is inside the
        exception message, which is exactly where the real content would be.
        """
        wrapped = run_agent.instrument_dispatch(
            Recorder([ToolError("customer_id 'CANARY-IN-EXC' is not valid")]))

        with self.assertRaises(ToolError):
            wrapped(object(), "8080", "crm_lookup", {}, "reference")

        span = self.spans()[0]
        haystack = repr([(e.name, dict(e.attributes or {})) for e in span.events])
        self.assertNotIn("CANARY-IN-EXC", haystack,
                         "the exception message reached the span as an event")
        self.assertEqual(len(span.events), 0,
                         f"the span carries events: {haystack}")

    # -----------------------------------------------------------------
    # Installation
    # -----------------------------------------------------------------

    def test_install_is_idempotent(self):
        """A second install must not produce two spans per dispatch.

        run_agent.install() already ran at import time, so this asserts the
        guard rather than setting it up.
        """
        before = run_agent.tools.dispatch
        run_agent.install()
        self.assertIs(run_agent.tools.dispatch, before,
                      "install() wrapped an already-wrapped dispatch")

    def test_the_installed_dispatch_is_the_real_one_underneath(self):
        """functools.wraps keeps the original reachable and identifiable."""
        self.assertIsNotNone(getattr(run_agent.tools.dispatch, "__wrapped__", None))
        self.assertEqual(run_agent.tools.dispatch.__name__, "dispatch")


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""The planner's model call becomes one GenAI chat span, names only.

The harness wraps the planner's session so each model request is a CLIENT span
Trustvian reads as a model call: category external, layer model, the request
model as its identity, and the usage counts task 087 reads. These tests pin the
exact attribute set, prove nothing carrying content reaches the span, and prove
the wrapper is a strict pass-through. No model and no network are involved.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind, StatusCode

from agent import planner as planner_mod
from harness import run_agent

URL = "http://ollama.localhost:11434/api/chat"

# Text that must never reach a span: a prompt, a completion and an error body.
PROMPT = "customer 42 says the invoice is wrong"
COMPLETION = '{"action": "finish", "reason": "secret completion text"}'
ERROR_BODY = "prediction aborted, token repeat limit reached"


class FakeResponse:
    def __init__(self, status_code=200, body=None, text=None):
        self.status_code = status_code
        self._body = body
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(
                f"{self.status_code} {ERROR_BODY}", response=self)


def ollama_reply(**fields):
    body = {"model": "gemma3:4b", "message": {"role": "assistant",
                                              "content": COMPLETION}}
    body.update(fields)
    return FakeResponse(body=body)


class RecordingSession:
    """A stand-in session that records each post and returns a fixed outcome."""

    def __init__(self, outcomes, tracer=None):
        self.outcomes = list(outcomes)
        self.posts = []
        self.tracer = tracer
        self.timeout = "delegated attribute"

    def post(self, url, *args, **kwargs):
        self.posts.append((url, args, kwargs))
        if self.tracer is not None:
            # What the requests instrumentation does inside session.post.
            with self.tracer.start_as_current_span("POST", kind=SpanKind.CLIENT):
                pass
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def payload(model="gemma3:4b"):
    return {"model": model, "stream": False, "format": {"type": "object"},
            "options": {"temperature": 0.0},
            "messages": [{"role": "system", "content": PROMPT}]}


class ModelCallSpan(unittest.TestCase):
    def setUp(self):
        self.exporter = InMemorySpanExporter()
        self.provider = TracerProvider()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self._saved = run_agent._model_tracer
        run_agent._model_tracer = self.provider.get_tracer("test")

    def tearDown(self):
        run_agent._model_tracer = self._saved
        self.provider.shutdown()

    def spans(self):
        return list(self.exporter.get_finished_spans())

    def chat_span(self):
        chats = [s for s in self.spans() if s.name.startswith("chat")]
        self.assertEqual(len(chats), 1, [s.name for s in self.spans()])
        return chats[0]

    def post(self, outcome, **kwargs):
        session = run_agent.ModelCallSession(RecordingSession([outcome]))
        return session.post(URL, json=payload(), timeout=180, **kwargs)

    # -----------------------------------------------------------------
    # The exact attribute set
    # -----------------------------------------------------------------

    def test_the_span_carries_exactly_the_named_attributes(self):
        self.post(ollama_reply(prompt_eval_count=812, eval_count=37))
        span = self.chat_span()
        self.assertEqual(span.name, "chat gemma3:4b")
        self.assertEqual(span.kind, SpanKind.CLIENT)
        self.assertEqual(dict(span.attributes), {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": "ollama",
            "gen_ai.request.model": "gemma3:4b",
            "gen_ai.response.model": "gemma3:4b",
            "server.address": "ollama.localhost",
            "server.port": 11434,
            "gen_ai.usage.input_tokens": 812,
            "gen_ai.usage.output_tokens": 37,
        })
        self.assertEqual(span.status.status_code, StatusCode.UNSET)
        self.assertEqual(len(span.events), 0)

    def test_an_absent_usage_count_is_left_unset_never_zero(self):
        cases = {
            "neither": ({}, set()),
            "input only": ({"prompt_eval_count": 5}, {"gen_ai.usage.input_tokens"}),
            "output only": ({"eval_count": 9}, {"gen_ai.usage.output_tokens"}),
            "not integers": ({"prompt_eval_count": "5", "eval_count": True}, set()),
            "negative": ({"prompt_eval_count": -1}, set()),
        }
        for name, (fields, present) in cases.items():
            with self.subTest(name):
                self.exporter.clear()
                self.post(ollama_reply(**fields))
                usage = {k for k in self.chat_span().attributes
                         if k.startswith("gen_ai.usage.")}
                self.assertEqual(usage, present)

    def test_a_stated_zero_is_reported_as_zero(self):
        self.post(ollama_reply(prompt_eval_count=0, eval_count=0))
        attributes = self.chat_span().attributes
        self.assertEqual(attributes["gen_ai.usage.input_tokens"], 0)
        self.assertEqual(attributes["gen_ai.usage.output_tokens"], 0)

    def test_the_response_model_comes_from_the_reply(self):
        self.post(ollama_reply(model="gemma3:4b-it-q4_K_M"))
        attributes = self.chat_span().attributes
        self.assertEqual(attributes["gen_ai.request.model"], "gemma3:4b")
        self.assertEqual(attributes["gen_ai.response.model"], "gemma3:4b-it-q4_K_M")

    # -----------------------------------------------------------------
    # Content never reaches the span
    # -----------------------------------------------------------------

    def assert_no_content(self):
        for span in self.spans():
            exported = json.dumps({
                "name": span.name,
                "attributes": dict(span.attributes),
                "status": span.status.description,
                "events": [(e.name, dict(e.attributes)) for e in span.events],
            })
            for text in (PROMPT, COMPLETION, "secret completion text",
                         ERROR_BODY, "boom"):
                self.assertNotIn(text, exported)

    def test_no_prompt_or_completion_reaches_the_span(self):
        self.post(ollama_reply(prompt_eval_count=3, eval_count=4))
        self.assert_no_content()

    def test_a_failed_reply_sets_a_fixed_error_status(self):
        self.post(FakeResponse(status_code=500, body={"error": ERROR_BODY}))
        span = self.chat_span()
        self.assertEqual(span.status.status_code, StatusCode.ERROR)
        self.assertEqual(span.status.description, run_agent.MODEL_CALL_FAILED)
        self.assertNotIn("gen_ai.response.model", span.attributes)
        self.assert_no_content()

    def test_an_exception_sets_a_fixed_error_status_and_propagates(self):
        error = requests.exceptions.ConnectionError("boom")
        with self.assertRaises(requests.exceptions.ConnectionError) as caught:
            self.post(error)
        self.assertIs(caught.exception, error)
        span = self.chat_span()
        self.assertEqual(span.status.status_code, StatusCode.ERROR)
        self.assertEqual(span.status.description, run_agent.MODEL_CALL_FAILED)
        self.assertEqual(len(span.events), 0)
        self.assert_no_content()

    def test_a_reply_that_is_not_json_adds_nothing(self):
        self.post(FakeResponse(text="<html>"))
        self.assertNotIn("gen_ai.response.model", self.chat_span().attributes)

    # -----------------------------------------------------------------
    # Pass-through
    # -----------------------------------------------------------------

    def test_arguments_and_response_pass_through_unchanged(self):
        reply = ollama_reply()
        inner = RecordingSession([reply])
        sent = payload()
        got = run_agent.ModelCallSession(inner).post(URL, json=sent, timeout=180)
        self.assertIs(got, reply)
        self.assertEqual(len(inner.posts), 1)
        url, args, kwargs = inner.posts[0]
        self.assertEqual((url, args), (URL, ()))
        self.assertIs(kwargs["json"], sent)
        self.assertEqual(kwargs["timeout"], 180)

    def test_other_attributes_are_delegated(self):
        inner = RecordingSession([])
        self.assertEqual(run_agent.ModelCallSession(inner).timeout,
                         "delegated attribute")

    def test_the_http_span_is_the_model_spans_child(self):
        inner = RecordingSession([ollama_reply()],
                                 tracer=self.provider.get_tracer("requests"))
        run_agent.ModelCallSession(inner).post(URL, json=payload())
        chat = self.chat_span()
        http = [s for s in self.spans() if s.name == "POST"]
        self.assertEqual(len(http), 1)
        self.assertEqual(http[0].parent.span_id, chat.context.span_id)

    # -----------------------------------------------------------------
    # Installed on the real planner
    # -----------------------------------------------------------------

    def test_the_planner_emits_one_chat_span_per_model_request(self):
        inner = RecordingSession([ollama_reply(prompt_eval_count=10, eval_count=2)])
        planner = planner_mod.Planner(inner, URL, model="gemma3:4b")
        self.assertIsInstance(planner.session, run_agent.ModelCallSession)
        action = planner.next_action(
            [{"role": "user", "content": PROMPT}], ["crm_lookup"])
        self.assertEqual(action["action"], "finish")
        self.assertEqual(len(inner.posts), 1)
        self.assertEqual(self.chat_span().attributes["gen_ai.usage.input_tokens"], 10)
        self.assert_no_content()

    def test_install_is_idempotent(self):
        before = planner_mod.Planner.__init__
        run_agent.install()
        self.assertIs(planner_mod.Planner.__init__, before)
        planner = planner_mod.Planner(RecordingSession([]), URL)
        self.assertIsInstance(planner.session._session, RecordingSession)


if __name__ == "__main__":
    unittest.main()

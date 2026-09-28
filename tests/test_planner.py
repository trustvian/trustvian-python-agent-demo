"""Tests for the planner. Uses a local stub HTTP server — never Ollama."""

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

from unittest.mock import Mock

from agent import planner as planner_mod
from agent import planner


class RawBody:
    """Wraps a reply that should be sent as-is, not inside a message envelope.

    Lets a test script a 200 response with a body shaped unlike Ollama's
    normal `{"message": {...}}` reply, e.g. one missing the `message` key.
    """

    def __init__(self, body):
        self.body = body


class StubOllama:
    """A local stand-in for Ollama's /api/chat, scripted per test."""

    def __init__(self, replies, status=200):
        self.replies = list(replies)
        self.status = status
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                outer.requests.append(json.loads(self.rfile.read(length)))
                if outer.status >= 400:
                    self.send_response(outer.status)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                content = outer.replies.pop(0) if outer.replies else "{}"
                if isinstance(content, RawBody):
                    body = json.dumps(content.body).encode()
                else:
                    body = json.dumps({"message": {"role": "assistant",
                                                   "content": content}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        # Threading, not plain HTTPServer: with HTTP/1.1 keep-alive the
        # single-threaded server parks inside the handler waiting for the
        # next request on a connection the client is still holding, and
        # shutdown() then blocks forever.
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/api/chat"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


REF_TOOLS = ("crm_lookup", "knowledge_search", "send_email")


class SchemaTest(unittest.TestCase):
    def test_schema_enum_is_the_tools_plus_finish(self):
        enum = planner.action_schema(REF_TOOLS)["properties"]["action"]["enum"]
        self.assertEqual(set(enum), set(REF_TOOLS) | {"finish"})

    def test_schema_omits_a_tool_not_offered(self):
        enum = planner.action_schema(REF_TOOLS)["properties"]["action"]["enum"]
        self.assertNotIn("export_customer", enum)

    def test_schema_has_no_open_arguments_object(self):
        props = planner.action_schema(REF_TOOLS)["properties"]
        self.assertNotIn("arguments", props)
        self.assertEqual(
            set(props),
            {"action", "customer_id", "query", "email_subject", "email_body",
             "reason"})

    def test_all_scalar_fields_are_required(self):
        self.assertEqual(set(planner.action_schema(REF_TOOLS)["required"]),
                         {"action", "customer_id", "query", "email_subject",
                          "email_body", "reason"})


class DefaultsTest(unittest.TestCase):
    def test_default_model_is_gemma3_4b(self):
        self.assertEqual(planner.DEFAULT_MODEL, "gemma3:4b")

    def test_default_url_addresses_ollama_by_hostname(self):
        self.assertEqual(planner.default_url(),
                         "http://ollama.localhost:11434/api/chat")


class NextActionTest(unittest.TestCase):
    def setUp(self):
        self.session = requests.Session()
        self.addCleanup(self.session.close)

    def test_valid_action_is_returned(self):
        stub = StubOllama(['{"action": "crm_lookup", "customer_id": "42", '
                           '"reason": "need the record"}'])
        self.addCleanup(stub.close)
        action = planner.Planner(self.session, stub.url).next_action(
            [{"role": "user", "content": "go"}], REF_TOOLS)
        self.assertEqual(action["action"], "crm_lookup")
        self.assertEqual(action["customer_id"], "42")

    def test_request_carries_the_schema_and_zero_temperature(self):
        stub = StubOllama(['{"action": "finish", "reason": "done"}'])
        self.addCleanup(stub.close)
        planner.Planner(self.session, stub.url).next_action(
            [{"role": "user", "content": "go"}], REF_TOOLS)
        sent = stub.requests[0]
        self.assertEqual(sent["model"], "gemma3:4b")
        self.assertIs(sent["stream"], False)
        self.assertEqual(sent["options"]["temperature"], 0)
        self.assertEqual(set(sent["format"]["properties"]["action"]["enum"]),
                         set(REF_TOOLS) | {"finish"})

    def test_model_override_is_honoured(self):
        stub = StubOllama(['{"action": "finish", "reason": "done"}'])
        self.addCleanup(stub.close)
        planner.Planner(self.session, stub.url, model="other:1b").next_action(
            [{"role": "user", "content": "go"}], REF_TOOLS)
        self.assertEqual(stub.requests[0]["model"], "other:1b")

    def test_malformed_json_is_retried_then_succeeds(self):
        stub = StubOllama(["not json at all",
                           '{"action": "finish", "reason": "done"}'])
        self.addCleanup(stub.close)
        action = planner.Planner(self.session, stub.url).next_action(
            [{"role": "user", "content": "go"}], REF_TOOLS)
        self.assertEqual(action["action"], "finish")
        self.assertEqual(len(stub.requests), 2)

    def test_retries_are_bounded(self):
        stub = StubOllama(["nope", "still nope", "nope again", "and again"])
        self.addCleanup(stub.close)
        with self.assertRaises(planner.PlannerError):
            planner.Planner(self.session, stub.url, max_retries=2).next_action(
                [{"role": "user", "content": "go"}], REF_TOOLS)
        # One initial attempt plus two retries, and no more.
        self.assertEqual(len(stub.requests), 3)

    def test_action_outside_the_enum_is_rejected(self):
        stub = StubOllama(['{"action": "export_customer", "reason": "x"}'] * 3)
        self.addCleanup(stub.close)
        with self.assertRaises(planner.PlannerError):
            planner.Planner(self.session, stub.url).next_action(
                [{"role": "user", "content": "go"}], REF_TOOLS)

    def test_a_json_list_is_rejected_then_corrected(self):
        stub = StubOllama(['[{"action": "finish"}]',
                           '{"action": "finish", "reason": "done"}'])
        self.addCleanup(stub.close)
        action = planner.Planner(self.session, stub.url).next_action(
            [{"role": "user", "content": "go"}], REF_TOOLS)
        self.assertEqual(action["action"], "finish")

    # Review Focus 2. An HTTP error is not a malformed reply, and a
    # correction message cannot fix it.
    #
    # A 5xx is now re-sent — identically, up to MAX_GENERATION_ATTEMPTS —
    # because Ollama returns one when the model aborts on a repetition loop, and
    # another sample does fix that. What must not happen is the thing this test
    # was written to prevent: treating it as a malformed reply and appending a
    # correction, which would teach the model about a mistake it did not make
    # and grow the conversation that caused the problem.
    def test_http_error_is_not_retried_as_malformed(self):
        stub = StubOllama([], status=500)
        self.addCleanup(stub.close)
        with self.assertRaises(planner.PlannerError) as ctx:
            planner.Planner(self.session, stub.url).next_action(
                [{"role": "user", "content": "go"}], REF_TOOLS)
        self.assertIn("Ollama", str(ctx.exception))

        # Bounded, and bounded by the generation budget rather than by the
        # malformed-reply budget — the two are separate on purpose.
        self.assertEqual(len(stub.requests),
                         planner.Planner.MAX_GENERATION_ATTEMPTS)

        # Every attempt carried the same messages. This is the assertion that
        # survives from the original: no correction was appended.
        sent = [request["messages"] for request in stub.requests]
        for messages in sent[1:]:
            self.assertEqual(messages, sent[0],
                             "a correction message was appended to a 5xx retry")

    # A 200 with an unusable body is a malformed reply, not a transport
    # failure — but it must still become a PlannerError, not a raw KeyError,
    # and it must not be retried as if a correction message could fix it.
    def test_a_response_missing_the_message_field_is_a_planner_error(self):
        stub = StubOllama([RawBody({"no_message_here": True})])
        self.addCleanup(stub.close)
        with self.assertRaises(planner.PlannerError):
            planner.Planner(self.session, stub.url).next_action(
                [{"role": "user", "content": "go"}], REF_TOOLS)
        self.assertEqual(len(stub.requests), 1)

    def test_correction_message_is_appended_for_the_retry(self):
        stub = StubOllama(["garbage", '{"action": "finish", "reason": "done"}'])
        self.addCleanup(stub.close)
        planner.Planner(self.session, stub.url).next_action(
            [{"role": "user", "content": "go"}], REF_TOOLS)
        first, second = stub.requests[0]["messages"], stub.requests[1]["messages"]
        self.assertGreater(len(second), len(first))
        self.assertEqual(second[-1]["role"], "user")

    def test_caller_messages_are_not_mutated(self):
        stub = StubOllama(["garbage", '{"action": "finish", "reason": "done"}'])
        self.addCleanup(stub.close)
        messages = [{"role": "user", "content": "go"}]
        planner.Planner(self.session, stub.url).next_action(messages, REF_TOOLS)
        self.assertEqual(len(messages), 1)

    # A 200 whose message.content is null passes _chat's own indexing (the
    # key is present), then json.loads(None) raises TypeError, which must
    # become a PlannerError, not escape the retry loop.
    def test_a_null_content_is_a_planner_error(self):
        stub = StubOllama(
            [RawBody({"message": {"role": "assistant", "content": None}})] * 3)
        self.addCleanup(stub.close)
        with self.assertRaises(planner.PlannerError):
            planner.Planner(self.session, stub.url).next_action(
                [{"role": "user", "content": "go"}], REF_TOOLS)

    # An action that is a list rather than a string must not reach the `in`
    # check against the allowed set, which raises TypeError on an unhashable
    # type instead of being rejected as a malformed reply.
    def test_a_non_string_action_is_rejected(self):
        stub = StubOllama(
            ['{"action": ["finish"], "reason": "x"}'] * 3)
        self.addCleanup(stub.close)
        with self.assertRaises(planner.PlannerError):
            planner.Planner(self.session, stub.url).next_action(
                [{"role": "user", "content": "go"}], REF_TOOLS)


if __name__ == "__main__":
    unittest.main()



class TemperatureTest(unittest.TestCase):
    """The knob a stability measurement needs, and its default.

    The default matters as much as the knob: everything documented about the
    reference run was measured at 0, and a story whose outcome changes between
    readings is not a story.
    """

    def setUp(self):
        self.session = requests.Session()
        self.addCleanup(self.session.close)
        # Saved and restored by hand rather than with mock.patch.dict, matching
        # the rest of this file's plain-unittest style.
        self.saved = os.environ.get("OLLAMA_TEMPERATURE")
        self.addCleanup(self.restore)

    def restore(self):
        if self.saved is None:
            os.environ.pop("OLLAMA_TEMPERATURE", None)
        else:
            os.environ["OLLAMA_TEMPERATURE"] = self.saved

    def given(self, value):
        if value is None:
            os.environ.pop("OLLAMA_TEMPERATURE", None)
        else:
            os.environ["OLLAMA_TEMPERATURE"] = value

    def finishing_stub(self):
        stub = StubOllama([json.dumps(
            {"action": "finish", "customer_id": "", "query": "",
             "email_subject": "", "email_body": "", "reason": "done"})])
        self.addCleanup(stub.close)
        return stub

    def test_the_default_is_zero(self):
        self.assertEqual(planner.DEFAULT_TEMPERATURE, 0.0)

    def test_an_unset_variable_is_the_default(self):
        self.given(None)
        self.assertEqual(planner.temperature_from_environment(), 0.0)

    def test_an_empty_variable_is_the_default(self):
        # An exported-but-empty variable is what an unset shell variable looks
        # like after `TEMPERATURE=$X make stability`, and it means "not given".
        self.given("")
        self.assertEqual(planner.temperature_from_environment(), 0.0)

    def test_a_value_is_read(self):
        self.given("0.7")
        self.assertEqual(planner.temperature_from_environment(), 0.7)

    def test_an_unparseable_value_is_fatal_rather_than_defaulted(self):
        # The one failure mode that must not be silent. A typo falling back to 0
        # would publish "an unchanged agent never fails a gate against itself"
        # about a configuration nobody asked to measure.
        for bad in ("warm", "0.7.1", "--"):
            with self.subTest(value=bad):
                self.given(bad)
                with self.assertRaises(planner.PlannerError):
                    planner.temperature_from_environment()

    def test_a_negative_value_is_refused(self):
        self.given("-1")
        with self.assertRaises(planner.PlannerError):
            planner.temperature_from_environment()

    def test_the_temperature_reaches_the_model_request(self):
        # The whole point: a knob that did not change the request would make
        # every stability figure a measurement of temperature 0.
        stub = self.finishing_stub()
        planner.Planner(self.session, stub.url, temperature=0.7).next_action(
            [{"role": "user", "content": "go"}], REF_TOOLS)
        self.assertEqual(stub.requests[-1]["options"]["temperature"], 0.7)

    def test_the_default_request_asks_for_zero(self):
        stub = self.finishing_stub()
        planner.Planner(self.session, stub.url).next_action(
            [{"role": "user", "content": "go"}], REF_TOOLS)
        self.assertEqual(stub.requests[-1]["options"]["temperature"], 0.0)


class GenerationFailureTest(unittest.TestCase):
    """A 5xx that says the model could not generate is retried; a dead server is not.

    This distinction is load-bearing rather than tidy. Ollama aborts generation
    with 500 {"error":"prediction aborted, token repeat limit reached"} when the
    model falls into a repetition loop, which gemma3:4b does at temperature 0.7
    roughly once in twelve calls on a long conversation. Treating it as fatal
    ended a 40-run stability sweep at repetition 7 of 20 with a working agent.
    """

    @staticmethod
    def _response(status, payload=None, body=""):
        response = Mock()
        response.status_code = status
        if payload is None:
            response.json.side_effect = ValueError("not json")
            response.text = body
        else:
            response.json.return_value = payload
        if status >= 400:
            response.raise_for_status.side_effect = \
                requests.exceptions.HTTPError(response=response)
        else:
            response.raise_for_status.return_value = None
        return response

    def _planner(self, responses):
        session = Mock()
        session.post.side_effect = responses
        return planner_mod.Planner(session, "http://ollama.localhost:11434/api/chat",
                                   temperature=0.7), session

    def test_a_generation_failure_is_retried_and_succeeds(self):
        good = self._response(200, {"message": {"content": '{"action": "finish"}'}})
        aborted = self._response(
            500, {"error": "prediction aborted, token repeat limit reached"})
        agent, session = self._planner([aborted, aborted, good])

        action = agent.next_action([{"role": "user", "content": "go"}], ("crm_lookup",))

        self.assertEqual(action["action"], "finish")
        self.assertEqual(session.post.call_count, 3,
                         "the identical request should have been re-sent")

    def test_the_retried_request_is_identical(self):
        """No correction message: the prompt was fine, only the sample was not.

        A correction here would teach the model something about a mistake it did
        not make, and would grow the conversation that caused the problem.
        """
        good = self._response(200, {"message": {"content": '{"action": "finish"}'}})
        aborted = self._response(500, {"error": "prediction aborted"})
        agent, session = self._planner([aborted, good])

        agent.next_action([{"role": "user", "content": "go"}], ("crm_lookup",))

        first, second = session.post.call_args_list
        self.assertEqual(first.kwargs["json"]["messages"],
                         second.kwargs["json"]["messages"])

    def test_generation_failures_are_bounded(self):
        aborted = [self._response(500, {"error": "prediction aborted"})
                   for _ in range(planner_mod.Planner.MAX_GENERATION_ATTEMPTS)]
        agent, session = self._planner(aborted)

        with self.assertRaises(planner_mod.PlannerError) as caught:
            agent.next_action([{"role": "user", "content": "go"}], ("crm_lookup",))

        self.assertEqual(session.post.call_count,
                         planner_mod.Planner.MAX_GENERATION_ATTEMPTS)
        self.assertIn("could not generate", str(caught.exception))

    def test_a_connection_failure_is_still_fatal_on_the_first_try(self):
        """The distinction the change exists to preserve.

        Another sample does not fix a server that is not there, and retrying it
        turns one clear error into several confusing ones.
        """
        session = Mock()
        session.post.side_effect = requests.exceptions.ConnectionError("refused")
        agent = planner_mod.Planner(session, "http://ollama.localhost:11434/api/chat")

        with self.assertRaises(planner_mod.PlannerError):
            agent.next_action([{"role": "user", "content": "go"}], ("crm_lookup",))
        self.assertEqual(session.post.call_count, 1)

    def test_a_4xx_is_still_fatal(self):
        """A bad request is not sampling luck, so it must not be retried."""
        agent, session = self._planner([self._response(400, {"error": "bad schema"})])

        with self.assertRaises(planner_mod.PlannerError):
            agent.next_action([{"role": "user", "content": "go"}], ("crm_lookup",))
        self.assertEqual(session.post.call_count, 1)

    def test_the_server_error_text_is_surfaced_and_bounded(self):
        agent, _ = self._planner(
            [self._response(500, {"error": "x" * 500})
             for _ in range(planner_mod.Planner.MAX_GENERATION_ATTEMPTS)])

        with self.assertRaises(planner_mod.PlannerError) as caught:
            agent.next_action([{"role": "user", "content": "go"}], ("crm_lookup",))
        message = str(caught.exception)
        self.assertIn("xxx", message)
        self.assertLess(len(message), 400, "the server's text should be truncated")

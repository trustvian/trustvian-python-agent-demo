"""Tests for the planner. Uses a local stub HTTP server — never Ollama."""

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

from agent import planner


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

    def test_action_and_reason_are_required(self):
        self.assertEqual(set(planner.action_schema(REF_TOOLS)["required"]),
                         {"action", "reason"})


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
    def test_http_error_is_not_retried_as_malformed(self):
        stub = StubOllama([], status=500)
        self.addCleanup(stub.close)
        with self.assertRaises(planner.PlannerError) as ctx:
            planner.Planner(self.session, stub.url).next_action(
                [{"role": "user", "content": "go"}], REF_TOOLS)
        self.assertIn("Ollama", str(ctx.exception))
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


if __name__ == "__main__":
    unittest.main()

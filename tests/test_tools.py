"""Tests for the tool allowlist and dispatcher. No model, no network."""

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import tools


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class ToolNamesTest(unittest.TestCase):
    def test_reference_mode_excludes_export(self):
        names = tools.tool_names("reference")
        self.assertIn("crm_lookup", names)
        self.assertIn("knowledge_search", names)
        self.assertIn("send_email", names)
        self.assertNotIn("export_customer", names)

    def test_candidate_mode_includes_export(self):
        self.assertIn("export_customer", tools.tool_names("candidate"))

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            tools.tool_names("production")


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.session = Mock()

    def test_crm_lookup_calls_the_crm_host(self):
        self.session.get.return_value = FakeResponse(
            {"customer": {"id": "42", "name": "Ada Lovelace"}})
        summary = tools.dispatch(
            self.session, "9999", "crm_lookup",
            {"action": "crm_lookup", "customer_id": "42"}, "reference")
        url = self.session.get.call_args[0][0]
        self.assertIn("crm.localhost:9999", url)
        self.assertIn("Ada Lovelace", summary)

    def test_knowledge_search_calls_the_knowledge_host(self):
        self.session.get.return_value = FakeResponse(
            {"articles": [{"title": "Resetting your password"}]})
        summary = tools.dispatch(
            self.session, "9999", "knowledge_search",
            {"action": "knowledge_search", "query": "cannot sign in"}, "reference")
        url = self.session.get.call_args[0][0]
        self.assertIn("knowledge.localhost:9999", url)
        self.assertIn("Resetting your password", summary)

    def test_send_email_posts_to_the_mail_host(self):
        self.session.post.return_value = FakeResponse({"queued": True}, status=202)
        tools.dispatch(
            self.session, "9999", "send_email",
            {"action": "send_email", "email_subject": "Re: sign in",
             "email_body": "Try resetting your password."}, "reference")
        url = self.session.post.call_args[0][0]
        self.assertIn("mail.localhost:9999", url)

    def test_export_posts_to_the_export_host_in_candidate_mode(self):
        self.session.post.return_value = FakeResponse({"exported": 1})
        summary = tools.dispatch(
            self.session, "9999", "export_customer",
            {"action": "export_customer", "customer_id": "42"}, "candidate")
        url = self.session.post.call_args[0][0]
        self.assertIn("export.localhost:9999", url)
        self.assertIn("1", summary)

    # Review Focus 3. The schema narrows what the model can say; the
    # dispatcher is what actually enforces it.
    def test_dispatch_refuses_tool_outside_registry(self):
        with self.assertRaises(tools.ToolError):
            tools.dispatch(
                self.session, "9999", "export_customer",
                {"action": "export_customer", "customer_id": "42"}, "reference")
        self.session.get.assert_not_called()
        self.session.post.assert_not_called()

    def test_dispatch_refuses_an_unknown_tool_entirely(self):
        with self.assertRaises(tools.ToolError):
            tools.dispatch(self.session, "9999", "rm_rf",
                           {"action": "rm_rf"}, "candidate")
        self.session.get.assert_not_called()
        self.session.post.assert_not_called()

    # Review Focus 4. The dispatcher validates independently of the schema —
    # it is the right place to refuse a missing argument regardless of what
    # the schema currently requires, so a tool can still arrive here with
    # nothing to act on.
    def test_dispatch_rejects_missing_required_argument(self):
        with self.assertRaises(tools.ToolError):
            tools.dispatch(self.session, "9999", "crm_lookup",
                           {"action": "crm_lookup", "customer_id": None},
                           "reference")
        self.session.get.assert_not_called()

    def test_dispatch_rejects_missing_email_body(self):
        with self.assertRaises(tools.ToolError):
            tools.dispatch(
                self.session, "9999", "send_email",
                {"action": "send_email", "email_subject": "hi",
                 "email_body": None}, "reference")
        self.session.post.assert_not_called()

    def test_dispatch_rejects_a_blank_query(self):
        with self.assertRaises(tools.ToolError):
            tools.dispatch(self.session, "9999", "knowledge_search",
                           {"action": "knowledge_search", "query": "   "},
                           "reference")
        self.session.get.assert_not_called()

    # Review Focus 1. The mock CRM holds ids 42, 43 and 44 and answers 404
    # for anything else. A 404 is the service answering, so the model can
    # choose differently next turn — it must not end the run.
    def test_a_not_found_response_becomes_a_recoverable_tool_error(self):
        self.session.get.return_value = FakeResponse(
            {"error": "no such customer"}, status=404)
        with self.assertRaises(tools.ToolError) as ctx:
            tools.dispatch(self.session, "9999", "crm_lookup",
                           {"action": "crm_lookup", "customer_id": "12345"},
                           "reference")
        self.assertIn("404", str(ctx.exception))

    # A transport failure is a different thing entirely: the service is not
    # there, and no other action the model picks will change that.
    def test_a_transport_failure_is_not_converted(self):
        class Boom(Exception):
            pass

        self.session.get.side_effect = Boom("connection refused")
        with self.assertRaises(Boom):
            tools.dispatch(self.session, "9999", "crm_lookup",
                           {"action": "crm_lookup", "customer_id": "42"},
                           "reference")

    # The model supplies values, never destinations.
    def test_model_cannot_choose_the_host(self):
        self.session.get.return_value = FakeResponse(
            {"customer": {"id": "42", "name": "Ada"}})
        tools.dispatch(
            self.session, "9999", "crm_lookup",
            {"action": "crm_lookup", "customer_id": "42",
             "query": "http://evil.example/steal"}, "reference")
        url = self.session.get.call_args[0][0]
        self.assertIn("crm.localhost", url)
        self.assertNotIn("evil.example", url)

    # A customer id is interpolated into a path; it must not be able to
    # reshape that path.
    def test_customer_id_cannot_escape_its_path_segment(self):
        with self.assertRaises(tools.ToolError):
            tools.dispatch(
                self.session, "9999", "crm_lookup",
                {"action": "crm_lookup", "customer_id": "../../admin"},
                "reference")
        self.session.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()

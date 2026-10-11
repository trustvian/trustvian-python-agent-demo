#!/usr/bin/env python3
"""Live mode's tools, mock routes and spans, with no model.

The mock services run in a thread; a scripted planner stands in for the model;
the harness's own dispatch wrapper and the requests instrumentation record
spans in memory. What is proven: each role's tickets produce the role's tool
spans, a drift ticket adds export_customer, CRM errors become refused tool
calls rather than crashes, the reference and candidate paths are unchanged,
and no ticket text, reply text or tool argument reaches a span.
"""

from __future__ import annotations

import importlib.util
import random
import sys
import threading
import unittest
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from opentelemetry.instrumentation.requests import RequestsInstrumentor  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

from agent import main as agent_main  # noqa: E402
from agent import planner as planner_mod  # noqa: E402
from agent import roles, tools  # noqa: E402
from harness import run_agent  # noqa: E402

_spec = importlib.util.spec_from_file_location("mock_server", ROOT / "mock_services" / "server.py")
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)

CUSTOMER = "44"
TICKET = "SENTINEL-LIVE-TICKET-81f3: the invoice for March is missing"
REPLY = "SENTINEL-LIVE-REPLY-2be7: here is how to fix it"
INVOICE = "inv-SENTINEL90aa"
PLANTED = (TICKET, REPLY, INVOICE)

# The schema as it was before live mode, for the paths measurements use.
REFERENCE_SCHEMA_REQUIRED = ["action", "customer_id", "query", "email_subject", "email_body", "reason"]


def setUpModule():
    global HTTPD, PORT, SESSION
    HTTPD = server.DualStackServer(("::", 0), server.Handler)
    PORT = str(HTTPD.server_address[1])
    threading.Thread(target=HTTPD.serve_forever, daemon=True).start()
    SESSION = requests.Session()


def tearDownModule():
    HTTPD.shutdown()
    HTTPD.server_close()
    SESSION.close()


def crm(method: str, path: str, body=None):
    response = SESSION.request(method, f"http://crm.localhost:{PORT}{path}", json=body, timeout=5)
    return response.status_code, response.json()


class ErrorRate:
    """Sets the mock services' error rate (and seed) inside a with block."""

    def __init__(self, rate: float, seed: int = 7):
        self.rate, self.seed = rate, seed

    def __enter__(self):
        server.ERROR_RATE, server.RNG = self.rate, random.Random(self.seed)

    def __exit__(self, *exc):
        server.ERROR_RATE = 0.0


class MockServices(unittest.TestCase):
    def test_no_error_rate_unless_set(self):
        # Every path but live mode starts the services without CRM_ERROR_RATE.
        self.assertEqual(server.ERROR_RATE, 0.0)
        self.assertTrue(all(crm("GET", "/crm/customers/42")[0] == 200 for _ in range(50)))

    def test_live_routes(self):
        status, body = crm("GET", "/invoices/42")
        self.assertEqual(status, 200)
        self.assertEqual([i["id"] for i in body["invoices"]], ["inv-4201", "inv-4202"])
        self.assertEqual(crm("GET", "/invoices/99")[0], 404)
        self.assertEqual(crm("POST", "/replies", {"customer_id": "42", "message": "hi"}), (201, {"status": "sent"}))
        self.assertEqual(crm("POST", "/refunds", {"customer_id": "42", "invoice_id": "inv-4202"}),
                         (201, {"status": "refunded"}))
        self.assertEqual(crm("POST", "/plans", {"customer_id": "43", "plan": "pro"}), (201, {"status": "changed"}))
        self.assertEqual(crm("POST", "/plans", {"customer_id": "43", "plan": "gold"})[0], 400)

    def test_every_customer_a_live_ticket_names_exists(self):
        for spec in roles.ROLES.values():
            for customer_id, _ in spec["tickets"]:
                self.assertIn(customer_id, server.CUSTOMERS)
                self.assertIn(customer_id, server.LIVE_INVOICES)

    def test_error_rate_one_fails_every_call_429_for_refunds(self):
        with ErrorRate(1.0):
            self.assertEqual(crm("GET", "/crm/customers/42")[0], 503)
            self.assertEqual(crm("POST", "/plans", {"customer_id": "42", "plan": "pro"})[0], 503)
            self.assertEqual(crm("POST", "/refunds", {"customer_id": "42", "invoice_id": "inv-4201"})[0], 429)
            # The readiness probe is never failed.
            self.assertEqual(SESSION.get(f"http://127.0.0.1:{PORT}/healthz", timeout=5).status_code, 200)

    def test_error_rate_is_the_share_of_failed_requests(self):
        same = random.Random(11)
        expected = sum(same.random() < 0.05 for _ in range(400))
        with ErrorRate(0.05, seed=11):
            failed = sum(crm("GET", "/invoices/42")[0] == 503 for _ in range(400))
        self.assertEqual(failed, expected)
        self.assertTrue(8 <= failed <= 35, failed)


class LiveTools(unittest.TestCase):
    def test_reference_and_candidate_tools_are_unchanged(self):
        self.assertEqual(tools.tool_names("reference"), (
            "crm_lookup", "knowledge_search", "billing_lookup", "account_history",
            "escalate_ticket", "attach_diagnostics", "share_with_partner", "send_email"))
        self.assertEqual(tools.tool_names("candidate"), tools.tool_names("reference") + ("export_customer",))

    def test_each_role_has_the_shared_tool_names(self):
        for role, spec in roles.ROLES.items():
            self.assertEqual(tools.tool_names(role), tuple(spec["tools"]))
        self.assertEqual(tools.tool_names("billing-drift")[-1], "export_customer")

    def test_the_reference_schema_is_unchanged(self):
        for mode in ("reference", "candidate"):
            schema = planner_mod.action_schema(tools.tool_names(mode))
            self.assertEqual(schema["required"], REFERENCE_SCHEMA_REQUIRED)
            self.assertEqual(set(schema["properties"]), set(REFERENCE_SCHEMA_REQUIRED))

    def test_the_live_schema_adds_only_what_its_tools_need(self):
        self.assertIn("message", planner_mod.action_schema(tools.tool_names("support"))["required"])
        billing = planner_mod.action_schema(tools.tool_names("billing"))["required"]
        self.assertIn("invoice_id", billing)
        self.assertNotIn("plan", billing)
        self.assertIn("plan", planner_mod.action_schema(tools.tool_names("accounts"))["required"])

    def test_live_tools_answer(self):
        def run(name, mode, **action):
            return tools.dispatch(SESSION, PORT, name, {"action": name, **action}, mode)

        self.assertEqual(run("lookup_customer", "support", customer_id="42"),
                         "customer 42 is Ada Lovelace, gold tier")
        self.assertEqual(run("send_reply", "support", customer_id="42", message="hi"), "reply sent")
        self.assertIn("inv-4401", run("lookup_invoice", "billing", customer_id="44"))
        self.assertEqual(run("issue_refund", "billing", customer_id="44", invoice_id="inv-4401"), "refund refunded")
        self.assertEqual(run("change_plan", "accounts", customer_id="43", plan="pro"), "plan changed")
        with self.assertRaises(tools.ToolError):  # not this role's tool
            run("change_plan", "support", customer_id="43", plan="pro")

    def test_a_crm_error_is_a_refused_tool_call(self):
        with ErrorRate(1.0):
            with self.assertRaisesRegex(tools.ToolError, "answered 503"):
                tools.dispatch(SESSION, PORT, "lookup_customer",
                               {"action": "lookup_customer", "customer_id": "42"}, "support")
            with self.assertRaisesRegex(tools.ToolError, "answered 429"):
                tools.dispatch(SESSION, PORT, "issue_refund",
                               {"action": "issue_refund", "customer_id": "42", "invoice_id": "inv-4201"},
                               "billing")


class ScriptedPlanner:
    """Answers with the next scripted action, as the model would."""

    def __init__(self, actions):
        self.actions = list(actions)
        self.offered = []

    def next_action(self, messages, tool_names):
        self.offered.append(tuple(tool_names))
        return self.actions.pop(0)


def act(name: str, **fields) -> dict:
    return {"action": name, "customer_id": CUSTOMER, "query": "", "email_subject": "",
            "email_body": "", "reason": "", **fields}


ROLE_ACTIONS = {
    "support": [act("lookup_customer"), act("send_reply", message=REPLY)],
    "billing": [act("lookup_customer"), act("lookup_invoice"), act("issue_refund", invoice_id=INVOICE)],
    "accounts": [act("lookup_customer"), act("change_plan", plan="team")],
}


class LiveSpans(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.exporter = InMemorySpanExporter()
        cls.provider = TracerProvider()
        cls.provider.add_span_processor(SimpleSpanProcessor(cls.exporter))
        cls._saved_tracer = run_agent._tracer
        run_agent._tracer = cls.provider.get_tracer("test")
        RequestsInstrumentor().instrument(tracer_provider=cls.provider)

    @classmethod
    def tearDownClass(cls):
        RequestsInstrumentor().uninstrument()
        run_agent._tracer = cls._saved_tracer
        cls.provider.shutdown()

    def setUp(self):
        self.exporter.clear()
        self._saved = {r: spec["tickets"] for r, spec in roles.ROLES.items()}
        for spec in roles.ROLES.values():
            spec["tickets"] = [(CUSTOMER, TICKET)]

    def tearDown(self):
        for role, pool in self._saved.items():
            roles.ROLES[role]["tickets"] = pool

    def run_ticket(self, role: str, drift: bool = False):
        ticket = roles.ticket(role, 20 if drift else 1, random.Random(0), drift_every=20)
        actions = list(ROLE_ACTIONS[role]) + ([act("export_customer")] if drift else []) + [act("finish")]
        planner = ScriptedPlanner(actions)
        calls = []
        agent_main.live_handler(SESSION, planner, PORT, calls)(ticket)
        return planner, calls, self.exporter.get_finished_spans()

    @staticmethod
    def tool_spans(spans):
        return [s.attributes["gen_ai.tool.name"] for s in spans if "gen_ai.tool.name" in s.attributes]

    def test_each_role_emits_its_tool_spans(self):
        for role in roles.ROLES:
            with self.subTest(role=role):
                self.exporter.clear()
                planner, calls, spans = self.run_ticket(role)
                names = [a["action"] for a in ROLE_ACTIONS[role]]
                self.assertEqual(self.tool_spans(spans), names)
                self.assertEqual(calls, [(n, True) for n in names])
                self.assertEqual(set(planner.offered), {tuple(roles.ROLES[role]["tools"])})

    def test_a_drift_ticket_adds_export_customer(self):
        planner, _, spans = self.run_ticket("billing", drift=True)
        self.assertEqual(self.tool_spans(spans)[-1], "export_customer")
        self.assertIn("export_customer", planner.offered[0])

    def test_a_failed_crm_call_is_recorded_as_failed_not_raised(self):
        with ErrorRate(1.0):
            _, calls, spans = self.run_ticket("support")
        self.assertEqual(calls, [("lookup_customer", False), ("send_reply", False)])
        self.assertEqual(self.tool_spans(spans), ["lookup_customer", "send_reply"])

    def test_no_content_reaches_a_span_in_live_mode(self):
        for role in roles.ROLES:
            with self.subTest(role=role):
                self.exporter.clear()
                _, _, spans = self.run_ticket(role, drift=role == "billing")
                self.assertTrue(spans)
                for span in spans:
                    for key, value in span.attributes.items():
                        text = str(value)
                        for planted in PLANTED:
                            self.assertNotIn(planted, text, (span.name, key))
                        # The customer id reaches only the URL of the request
                        # the tool makes (/crm/customers/<id>, /invoices/<id>).
                        if f"/{CUSTOMER}" in text or text == CUSTOMER:
                            self.assertIn(key, ("http.url", "url.full", "url.path", "http.target"),
                                          (span.name, key))
                    for planted in PLANTED:
                        self.assertNotIn(planted, span.name)


if __name__ == "__main__":
    unittest.main()

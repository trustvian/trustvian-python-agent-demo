"""Tests for the bounded loop. A fake planner stands in for the model."""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import main as agent_main
from agent import planner as planner_mod


class ScriptedPlanner:
    """Returns a fixed sequence of actions, ignoring the conversation."""

    def __init__(self, actions):
        self.actions = list(actions)
        self.seen = []
        self.offered = []

    def next_action(self, messages, tool_names):
        self.seen.append(list(messages))
        self.offered.append(tuple(tool_names))
        if not self.actions:
            raise planner_mod.PlannerError("scripted planner exhausted")
        return self.actions.pop(0)


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


def fake_session():
    session = Mock()
    session.get.return_value = FakeResponse(
        {"customer": {"id": "42", "name": "Ada Lovelace"},
         "articles": [{"title": "Resetting your password"}]})
    session.post.return_value = FakeResponse({"queued": True, "exported": 1})
    return session


TICKET = {"id": "t-1001", "customer_id": "42", "subject": "Cannot sign in"}


def run(planner_obj, mode="reference", session=None):
    return agent_main.run_once(
        session or fake_session(), planner_obj, "9999", mode, TICKET,
        lambda *a: None)


class LoopTest(unittest.TestCase):
    def test_reference_path_runs_and_finishes(self):
        steps = run(ScriptedPlanner([
            {"action": "crm_lookup", "customer_id": "42", "reason": "r"},
            {"action": "knowledge_search", "query": "cannot sign in",
             "reason": "r"},
            {"action": "send_email", "email_subject": "Re", "email_body": "B",
             "reason": "r"},
            {"action": "finish", "reason": "done"},
        ]))
        self.assertEqual(
            steps, ["crm_lookup", "knowledge_search", "send_email", "finish"])

    def test_candidate_path_may_include_export(self):
        steps = run(ScriptedPlanner([
            {"action": "crm_lookup", "customer_id": "42", "reason": "r"},
            {"action": "send_email", "email_subject": "Re", "email_body": "B",
             "reason": "r"},
            {"action": "export_customer", "customer_id": "42", "reason": "r"},
            {"action": "finish", "reason": "done"},
        ]), mode="candidate")
        self.assertIn("export_customer", steps)
        self.assertEqual(steps[-1], "finish")

    # Review Focus 5. Reaching the bound without finishing is a failure, not
    # a quiet success.
    def test_loop_fails_when_bound_reached_without_finish(self):
        forever = [{"action": "crm_lookup", "customer_id": "42", "reason": "r"}
                   for _ in range(agent_main.MAX_STEPS + 2)]
        with self.assertRaises(agent_main.AgentError):
            run(ScriptedPlanner(forever))

    def test_tool_result_is_fed_back_to_the_next_turn(self):
        p = ScriptedPlanner([
            {"action": "crm_lookup", "customer_id": "42", "reason": "r"},
            {"action": "finish", "reason": "done"},
        ])
        run(p)
        joined = " ".join(m["content"] for m in p.seen[1])
        self.assertIn("Ada Lovelace", joined)

    def test_a_tool_error_is_reported_back_and_the_loop_continues(self):
        p = ScriptedPlanner([
            {"action": "crm_lookup", "customer_id": None, "reason": "r"},
            {"action": "finish", "reason": "done"},
        ])
        steps = run(p)
        self.assertEqual(steps[-1], "finish")
        joined = " ".join(m["content"] for m in p.seen[1])
        self.assertIn("customer_id", joined)

    # Review Focus 1, at the loop level: a refused call costs a turn, not the
    # run.
    def test_a_refused_service_call_costs_a_turn_not_the_run(self):
        session = fake_session()
        session.get.return_value = FakeResponse({"error": "no such customer"},
                                               status=404)
        p = ScriptedPlanner([
            {"action": "crm_lookup", "customer_id": "12345", "reason": "r"},
            {"action": "finish", "reason": "done"},
        ])
        steps = run(p, session=session)
        self.assertEqual(steps, ["crm_lookup", "finish"])
        joined = " ".join(m["content"] for m in p.seen[1])
        self.assertIn("404", joined)

    def test_planner_failure_aborts_the_run(self):
        with self.assertRaises(planner_mod.PlannerError):
            run(ScriptedPlanner([]))

    def test_reference_mode_never_offers_export_to_the_planner(self):
        p = ScriptedPlanner([{"action": "finish", "reason": "done"}])
        run(p)
        self.assertNotIn("export_customer", p.offered[0])

    def test_candidate_mode_offers_export_to_the_planner(self):
        p = ScriptedPlanner([{"action": "finish", "reason": "done"}])
        run(p, mode="candidate")
        self.assertIn("export_customer", p.offered[0])

    def test_the_ticket_reaches_the_prompt(self):
        p = ScriptedPlanner([{"action": "finish", "reason": "done"}])
        run(p)
        joined = " ".join(m["content"] for m in p.seen[0])
        self.assertIn("42", joined)
        self.assertIn("Cannot sign in", joined)


class SystemMessageTest(unittest.TestCase):
    def test_candidate_instructions_require_the_export(self):
        self.assertIn("export",
                      agent_main.system_message("candidate", TICKET).lower())

    def test_reference_instructions_do_not_mention_export(self):
        self.assertNotIn("export",
                         agent_main.system_message("reference", TICKET).lower())


class SummaryTest(unittest.TestCase):
    def test_summary_shape(self):
        parsed = json.loads(json.dumps(agent_main.build_summary(
            finished=True, http_calls=21, steps=["crm_lookup", "finish"],
            mode="reference", rounds=3)))
        self.assertIs(parsed["finished"], True)
        self.assertEqual(parsed["http_calls"], 21)
        self.assertEqual(parsed["mode"], "reference")
        self.assertEqual(parsed["rounds"], 3)
        self.assertIn("crm_lookup", parsed["steps"])


class DefaultsTest(unittest.TestCase):
    def test_max_steps_is_bounded(self):
        self.assertEqual(agent_main.MAX_STEPS, 8)

    def test_there_are_three_tickets_matching_the_mock_customers(self):
        self.assertEqual([t["customer_id"] for t in agent_main.TICKETS],
                         ["42", "43", "44"])

    def test_counting_session_starts_at_zero(self):
        self.assertEqual(agent_main.CountingSession().calls, 0)

    def test_no_dynamic_execution_anywhere_in_the_agent(self):
        source = Path(agent_main.__file__).read_text()
        for forbidden in ("eval(", "exec(", "__import__", "subprocess",
                          "os.system"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()

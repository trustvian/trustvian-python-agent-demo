#!/usr/bin/env python3
"""Runs the support agent with tool calls instrumented, without touching it.

This is the harness, not the application. It stands in for what a framework's
zero-code instrumentation would have provided: the agent dispatches tools through
one function, and this wraps that function so each dispatch becomes an
OpenTelemetry GenAI ``execute_tool`` span. Trustvian then reads the tool's own
name instead of the transport underneath it.

Why it exists as a third directory, beside ``agent/`` and ``tools/``:

  agent/     the application under test. Imports neither Trustvian nor
             OpenTelemetry, and `make smoke` asserts it.
  tools/     the demo's own tooling, in a separate interpreter that holds
             PyYAML and nothing else — it cannot import `requests`, so it
             cannot import agent.tools, so the wrapper cannot live there.
  harness/   this. Runs in the agent's interpreter, imports OpenTelemetry,
             and is imported by nothing under agent/ or fixtures/.

The invariant that matters is unchanged: the application under test imports
neither Trustvian nor OpenTelemetry. This file is not the application.

What makes the wrapping legitimate rather than a modification is that it is a
strict pass-through — see ``instrument_dispatch``. Trustvian's task 080 sets the
rule: wrapping a dispatch function from the harness at import time is allowed,
where editing source, forking, vendoring, or altering behavior or order is not.
"""

from __future__ import annotations

import functools
import pathlib
import sys

# The repository root, so `from agent import ...` resolves when this file is run
# as a script. Inserted rather than appended: a same-named module elsewhere on
# the path would otherwise shadow the application under test.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from opentelemetry import trace  # noqa: E402

from agent import tools  # noqa: E402

# The two attributes, and only these two.
#
# OpenTelemetry's GenAI convention; Trustvian's internal/semconv reads
# gen_ai.operation.name to decide the convention applies and gen_ai.tool.name as
# the operation's identity. Task 080 names exactly this pair and excludes the
# rest: no gen_ai.tool.description, no gen_ai.tool.definitions, no argument,
# return-value or error attribute.
#
# gen_ai.agent.name is deliberately absent. It would raise the actor type from
# `service` to `ai_agent`, which is what the actor *is* — but ActorType is one of
# the six StableFeatures dimensions, so adding it changes every fingerprint and
# discards the learned baseline. That belongs in its own change, not in the one
# that moves fidelity. See the follow-up issue.
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
EXECUTE_TOOL = "execute_tool"

_tracer = trace.get_tracer("trustvian-demo.harness.tools")


def instrument_dispatch(dispatch):
    """Wrap a dispatch function so each call emits one execute_tool span.

    A strict pass-through. Every one of these properties is asserted by
    tests/test_wrapper_passthrough.py, with no model involved:

      - arguments reach the wrapped function by value and position, unchanged;
      - the return value is returned unchanged;
      - every exception propagates with its type and payload intact;
      - one call in is one call out — never retried, reordered, batched, cached
        or suppressed;
      - exactly one execute_tool span per dispatch, carrying the tool name and
        no argument, return-value or description attribute.

    The span is the *parent* of the HTTP span, not a child of it. The HTTP span
    is created inside `dispatch`, by the requests instrumentation, when
    session.get or session.post runs — so a wrapper around dispatch necessarily
    opens before it and closes after. Both spans exist, which is the point: the
    mock-service call stays visible beneath the tool that made it.

    record_exception=False is not a detail. The default attaches str(exc) to the
    span as an event, and a ToolError message quotes the model's own argument —
    agent.tools._require_id raises "customer_id 'x' is not a valid identifier".
    That is content, and it would travel. The status is still set on failure,
    which is the content-free half and the half Trustvian reads as a signal.
    """
    @functools.wraps(dispatch)
    def wrapper(session, port, name, action, mode):
        with _tracer.start_as_current_span(
            f"{EXECUTE_TOOL} {name}",
            record_exception=False,
            attributes={
                GEN_AI_OPERATION_NAME: EXECUTE_TOOL,
                GEN_AI_TOOL_NAME: name,
            },
        ):
            return dispatch(session, port, name, action, mode)

    return wrapper


def install() -> None:
    """Rebind agent.tools.dispatch to its instrumented form.

    Idempotent: a second call is a no-op rather than a second layer of spans,
    because a doubly-wrapped dispatch would report every tool call twice and read
    as an actor behaving strangely.

    Rebinding the module attribute is sufficient because agent/main.py calls
    `tools.dispatch(...)` — a module lookup at call time — so nothing in the
    application has to cooperate, and nothing in it is edited.
    """
    if getattr(tools.dispatch, "__wrapped__", None) is not None:
        return
    tools.dispatch = instrument_dispatch(tools.dispatch)


install()

from agent import main as agent_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(agent_main.main())

#!/usr/bin/env python3
"""Runs the support agent with tool and model calls instrumented, without touching it.

This is the harness, not the application. It stands in for what a framework's
zero-code instrumentation would have provided:

  - the agent dispatches tools through one function, and this wraps that
    function so each dispatch becomes an OpenTelemetry GenAI ``execute_tool``
    span. Trustvian then reads the tool's own name instead of the transport
    underneath it;
  - the planner sends each model request through the session it is given, and
    this wraps that session so each request becomes a GenAI ``chat`` span. An
    LLM client library's instrumentation would emit the same span; the planner
    uses plain ``requests``, so without this Trustvian sees only
    ``POST → ollama.localhost`` at transport fidelity — no model layer, no
    tokens.

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
from urllib.parse import urlsplit

# The repository root, so `from agent import ...` resolves when this file is run
# as a script. Inserted rather than appended: a same-named module elsewhere on
# the path would otherwise shadow the application under test.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from opentelemetry import trace  # noqa: E402
from opentelemetry.trace import SpanKind, Status, StatusCode  # noqa: E402

from agent import planner as planner_mod  # noqa: E402
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

    Two SDK defaults are switched off, because each copies an exception's
    message onto the span and a ToolError message quotes the model's own
    argument — agent.tools._require_id raises "customer_id 'x' is not a valid
    identifier". That is content, and it would travel:

      record_exception=False          no exception event carrying str(exc);
      set_status_on_exception=False   no status description "Type: str(exc)".

    The status itself is still set on failure — ERROR, with no description —
    because the code is the content-free half and the half Trustvian reads as a
    signal. The exception is re-raised as the same object, unchanged.
    """
    @functools.wraps(dispatch)
    def wrapper(session, port, name, action, mode):
        with _tracer.start_as_current_span(
            f"{EXECUTE_TOOL} {name}",
            record_exception=False,
            set_status_on_exception=False,
            attributes={
                GEN_AI_OPERATION_NAME: EXECUTE_TOOL,
                GEN_AI_TOOL_NAME: name,
            },
        ) as span:
            try:
                return dispatch(session, port, name, action, mode)
            except BaseException:
                span.set_status(Status(StatusCode.ERROR))
                raise

    return wrapper


def install() -> None:
    """Rebind agent.tools.dispatch and Planner.__init__ to instrumented forms.

    Idempotent: a second call is a no-op rather than a second layer of spans,
    because a doubly-wrapped dispatch would report every tool call twice and read
    as an actor behaving strangely. The same holds for the model call.

    Rebinding the module attribute is sufficient because agent/main.py calls
    `tools.dispatch(...)` — a module lookup at call time — and constructs its
    planner after this runs, so nothing in the application has to cooperate,
    and nothing in it is edited.
    """
    if getattr(tools.dispatch, "__wrapped__", None) is None:
        tools.dispatch = instrument_dispatch(tools.dispatch)
    if getattr(planner_mod.Planner.__init__, "__wrapped__", None) is None:
        planner_mod.Planner.__init__ = instrument_planner(planner_mod.Planner.__init__)


# The model call's attributes, and only these.
#
# OpenTelemetry's GenAI client-span convention, names only. Trustvian's
# internal/semconv reads gen_ai.operation.name=chat as a model call (category
# external, layer model), gen_ai.request.model as its identity and
# gen_ai.provider.name as its target; task 087 reads the two usage counts.
# server.address and server.port name the endpoint, as the convention asks of a
# client span. Nothing that carries content is set: no gen_ai.input.messages,
# gen_ai.output.messages, gen_ai.system_instructions, prompt, completion or tool
# argument. gen_ai.agent.name stays absent for the reason given above.
GEN_AI_PROVIDER_NAME = "gen_ai.provider.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
SERVER_ADDRESS = "server.address"
SERVER_PORT = "server.port"
CHAT = "chat"
OLLAMA = "ollama"

# Ollama's response fields for the two usage counts. A reply is not guaranteed
# to carry either — an error body carries neither — and an absent count is left
# unset, never reported as 0: "not stated" and "zero tokens" are different facts
# to Trustvian's task 087.
_USAGE_FIELDS = (
    ("prompt_eval_count", GEN_AI_USAGE_INPUT_TOKENS),
    ("eval_count", GEN_AI_USAGE_OUTPUT_TOKENS),
)

# The status description on a failed model call. Fixed, for the same reason the
# tool span records no exception: an exception message or Ollama's error body is
# text the span must not carry.
MODEL_CALL_FAILED = "model call failed"

_model_tracer = trace.get_tracer("trustvian-demo.harness.model")


def _request_attributes(url, payload):
    """The attributes known before the request is sent."""
    attributes = {
        GEN_AI_OPERATION_NAME: CHAT,
        GEN_AI_PROVIDER_NAME: OLLAMA,
    }
    model = payload.get("model") if isinstance(payload, dict) else None
    if isinstance(model, str) and model:
        attributes[GEN_AI_REQUEST_MODEL] = model
    parts = urlsplit(url)
    if parts.hostname:
        attributes[SERVER_ADDRESS] = parts.hostname
    if parts.port is not None:
        attributes[SERVER_PORT] = parts.port
    return attributes


def _response_attributes(response):
    """The response model and usage counts, each only when the reply states it.

    Reads three named fields of Ollama's reply and nothing else — never
    message.content. A reply that is not a JSON object yields nothing.
    """
    try:
        body = response.json()
    except ValueError:
        return {}
    if not isinstance(body, dict):
        return {}
    attributes = {}
    model = body.get("model")
    if isinstance(model, str) and model:
        attributes[GEN_AI_RESPONSE_MODEL] = model
    for field, attribute in _USAGE_FIELDS:
        value = body.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            attributes[attribute] = value
    return attributes


class ModelCallSession:
    """The planner's session, with each post a GenAI ``chat`` CLIENT span.

    A strict pass-through, like instrument_dispatch: the post reaches the real
    session with the same arguments, its response is returned unchanged, and an
    exception propagates as the same object. Everything else is delegated, so
    the application's own CountingSession still counts every request.

    The span is the *parent* of the HTTP span the requests instrumentation
    creates inside session.post, so both exist: the model call, and the
    transport request beneath it.
    """

    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def post(self, url, *args, **kwargs):
        attributes = _request_attributes(url, kwargs.get("json"))
        model = attributes.get(GEN_AI_REQUEST_MODEL, "")
        with _model_tracer.start_as_current_span(
            f"{CHAT} {model}".rstrip(),
            kind=SpanKind.CLIENT,
            record_exception=False,
            set_status_on_exception=False,
            attributes=attributes,
        ) as span:
            try:
                response = self._session.post(url, *args, **kwargs)
            except BaseException:
                span.set_status(Status(StatusCode.ERROR, MODEL_CALL_FAILED))
                raise
            span.set_attributes(_response_attributes(response))
            if response.status_code >= 400:
                span.set_status(Status(StatusCode.ERROR, MODEL_CALL_FAILED))
            return response


def instrument_planner(init):
    """Wrap Planner.__init__ so the planner's session is a ModelCallSession.

    Only the planner's view of the session changes. The object the application
    created is the one every request still goes through, and the tool dispatch
    path, which receives that object directly, is untouched.
    """
    @functools.wraps(init)
    def wrapper(self, session, *args, **kwargs):
        if not isinstance(session, ModelCallSession):
            session = ModelCallSession(session)
        init(self, session, *args, **kwargs)

    return wrapper


install()

from agent import main as agent_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(agent_main.main())

"""Asks the local model for the next action, and refuses anything else.

The model is constrained twice: the request carries a JSON Schema listing the
exact actions available this turn, and the reply is checked again here before
the caller sees it — that it parses as JSON, is an object, and names an
action inside this turn's allowlist. The schema is a strong hint, not a
guarantee, so the second check is not redundant.

Nothing here dispatches anything. This module's whole output is a validated
action dictionary.
"""

from __future__ import annotations

import json
import os

import requests

DEFAULT_MODEL = "gemma3:4b"

# Temperature 0 is this agent's default and stays it.
#
# Everything documented about the reference run — which actions the model picks,
# how many turns it takes, the observed record counts — was measured at 0, and a
# story whose outcome changes between readings is not a story.
#
# It is configurable because *measuring* nondeterminism needs a configuration
# somebody would actually ship, and nobody ships an agent pinned at 0 to make its
# behavior reproducible. 0.7 is a common application-level default, below
# Ollama's own server default of 0.8 and the OpenAI API's 1.0, so instability
# measured there is a conservative figure rather than a worst case.
DEFAULT_TEMPERATURE = 0.0
OLLAMA_HOST = "ollama.localhost"
OLLAMA_PORT = 11434

# Long enough for a cold model load on a laptop, which is the slow case.
REQUEST_TIMEOUT = 180

FINISH = "finish"


class PlannerError(Exception):
    """The model could not be asked, or did not answer usably."""


class PlannerUnusableReply(PlannerError):
    """The model failed to produce an answer, and another sample may succeed.

    Distinct from PlannerError because the two need opposite handling. A refused
    connection, a DNS failure or a timeout is not something another sample fixes,
    and retrying it turns one clear error into several confusing ones. A server
    that answered and said it could not generate is the opposite: the prompt is
    fine, the sample was not, and the fix is to ask again.

    Measured rather than assumed. Ollama aborts generation with

        500 {"error":"prediction aborted, token repeat limit reached"}

    when the model falls into a repetition loop, which gemma3:4b does at
    temperature 0.7 roughly once in twelve calls once a ticket's conversation
    grows past ~1000 tokens. Treating that as fatal ended a 40-run stability
    sweep at repetition 7 of 20, with an agent that was working correctly.
    """


def default_url() -> str:
    """The local chat endpoint.

    Addressed by hostname rather than by IP so the call is as identifiable in
    logs as every other backend this agent talks to.
    """
    return f"http://{OLLAMA_HOST}:{OLLAMA_PORT}/api/chat"


def model_from_environment() -> str:
    """The configured model, defaulting to the one this demo is built around."""
    return os.environ.get("OLLAMA_MODEL") or DEFAULT_MODEL


def temperature_from_environment() -> float:
    """The configured sampling temperature, defaulting to 0.

    An unreadable value is a hard failure rather than a silent fall back to the
    default. A stability measurement's whole output is a number that depends on
    this one, and a typo that quietly produced a temperature-0 sweep would
    publish "an unchanged agent never fails a gate" about a configuration nobody
    asked for — which is the one result this must not report by accident.
    """
    raw = os.environ.get("OLLAMA_TEMPERATURE")
    if raw is None or raw.strip() == "":
        return DEFAULT_TEMPERATURE
    try:
        value = float(raw)
    except ValueError:
        raise PlannerError(
            f"OLLAMA_TEMPERATURE={raw!r} is not a number") from None
    if value < 0:
        raise PlannerError(f"OLLAMA_TEMPERATURE={raw!r} is negative")
    return value


def _server_error(response) -> str:
    """Ollama's own error text, when it sent one.

    Bounded and not logged anywhere: this reaches a developer's terminal through
    an exception message, and the field is Ollama's rather than the model's, so it
    carries no completion. Truncated because a server is free to send anything.
    """
    if response is None:
        return ""
    try:
        message = response.json().get("error", "")
    except ValueError:
        message = response.text
    return str(message)[:200]


def action_schema(tool_names) -> dict:
    """The response schema for one turn.

    `action` is a closed enum of exactly the tools available now plus finish,
    and every other field is a bounded scalar. There is deliberately no open
    arguments object: an open map is a place for the model to invent structure
    the dispatcher then has to interpret.

    The four scalar fields are required strings, not optional ones, even
    though a given action only reads some of them. They used to be nullable
    so a tool could simply omit what it does not need — but measured against
    the real model, a small model at temperature 0 answers an optional field
    with `null` every time, rather than filling in the one its chosen action
    actually needs. The action then gets refused for want of an argument it
    plainly had available (the ticket's own customer id, say), and the run
    never recovers. Requiring the field costs the model a throwaway value on
    an action that ignores it, which nothing downstream reads — and that is
    cheaper than an action that can never run.
    """
    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(tool_names) + [FINISH]},
            "customer_id": {"type": "string"},
            "query": {"type": "string"},
            "email_subject": {"type": "string"},
            "email_body": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["action", "customer_id", "query", "email_subject",
                     "email_body", "reason"],
    }


class Planner:
    """One model, one endpoint, one action per call."""

    # Attempts allowed when the server answers but reports it could not
    # generate. Separate from max_retries, and larger, because the two failures
    # have different rates.
    #
    # A malformed reply is the model misunderstanding the schema, and a
    # correction usually lands on the first retry. A repetition-loop abort is
    # sampling luck at ~8% per call on a long conversation, so three attempts
    # leave 0.05% per call — which over the ~500 calls of a two-configuration
    # sweep is a 23% chance of losing a repetition. Five attempts put that at
    # 0.16%, which is small enough that a failure means something real rather
    # than arithmetic catching up.
    MAX_GENERATION_ATTEMPTS = 5

    def __init__(self, session, url, model=DEFAULT_MODEL, max_retries=2,
                 temperature=DEFAULT_TEMPERATURE):
        self.session = session
        self.url = url
        self.model = model
        self.max_retries = max_retries
        self.temperature = temperature

    def next_action(self, messages, tool_names) -> dict:
        """Ask for one action and return it validated.

        What is re-checked here is that the reply parses as JSON, is an
        object, and names an action inside `tool_names` plus finish. The
        schema also asks the model for a `reason`, but nothing downstream
        consumes it, so its presence is not enforced on the way back —
        rejecting a reply over an unused field would spend a retry for no
        behavioural gain.

        `messages` is not modified: a correction for a malformed reply is
        appended to a local copy, so a caller's conversation never silently
        grows the agent's own error handling.
        """
        schema = action_schema(tool_names)
        allowed = set(tool_names) | {FINISH}
        attempt = list(messages)
        problem = "no attempt was made"

        for _ in range(self.max_retries + 1):
            # A generation failure is not a malformed reply and costs no
            # correction message: the prompt is unchanged and only the sample
            # was bad, so this retries the identical request. It does not consume
            # an outer attempt either — an outer attempt is for teaching the
            # model something, and there is nothing to teach here.
            try:
                content = self._chat_with_generation_retry(attempt, schema)
            except PlannerUnusableReply as exc:
                raise PlannerError(
                    f"the model could not generate a reply after "
                    f"{self.MAX_GENERATION_ATTEMPTS} attempts: {exc}") from exc
            try:
                action = json.loads(content)
            except (ValueError, TypeError):
                problem = "the reply was not valid JSON"
                attempt = attempt + [{
                    "role": "user",
                    "content": ("Your previous reply was not valid JSON. Reply "
                                "with one JSON object matching the schema."),
                }]
                continue

            if not isinstance(action, dict):
                problem = "the reply was not a JSON object"
                attempt = attempt + [{
                    "role": "user",
                    "content": ("Reply with a single JSON object, not a list "
                                "or a scalar."),
                }]
                continue

            name = action.get("action")
            if not isinstance(name, str) or name not in allowed:
                problem = f"{name!r} is not an available action"
                attempt = attempt + [{
                    "role": "user",
                    "content": (f"{name!r} is not available. Choose exactly one "
                                f"of: {', '.join(sorted(allowed))}."),
                }]
                continue

            return action

        raise PlannerError(
            f"the model did not return a usable action after "
            f"{self.max_retries + 1} attempts: {problem}")

    def _chat_with_generation_retry(self, messages, schema) -> str:
        """One reply, retrying only a server-side failure to generate.

        Bounded and unconditional in its delay: there is no backoff, because the
        cause is sampling rather than load, and a local model server under no
        contention gains nothing from waiting.
        """
        problem = "no attempt was made"
        for _ in range(self.MAX_GENERATION_ATTEMPTS):
            try:
                return self._chat(messages, schema)
            except PlannerUnusableReply as exc:
                problem = str(exc)
        raise PlannerUnusableReply(problem)

    def _chat(self, messages, schema) -> str:
        """One request. An HTTP failure is fatal, not something to retry here.

        A correction message can fix a malformed reply; it cannot fix a
        refused connection or a 500, and retrying those with a politer prompt
        would turn one clear error into three confusing ones. That failure is
        caught narrowly, so a bug in this method is not misreported as an
        Ollama problem.
        """
        payload = {
            "model": self.model,
            "stream": False,
            "format": schema,
            "options": {"temperature": self.temperature},
            "messages": messages,
        }
        try:
            response = self.session.post(
                self.url, json=payload, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            # 5xx means the server was reached and could not answer. That is a
            # different fact from "the server is not there", and only one of the
            # two is worth another sample.
            status = exc.response.status_code if exc.response is not None else 0
            if 500 <= status < 600:
                raise PlannerUnusableReply(
                    f"Ollama could not generate a reply: {status} "
                    f"{_server_error(exc.response)}") from exc
            raise PlannerError(f"Ollama request failed: {exc}") from exc
        except requests.exceptions.RequestException as exc:
            raise PlannerError(f"Ollama request failed: {exc}") from exc

        try:
            return response.json()["message"]["content"]
        except (KeyError, ValueError, TypeError) as exc:
            raise PlannerError(f"Ollama reply was not usable: {exc}") from exc

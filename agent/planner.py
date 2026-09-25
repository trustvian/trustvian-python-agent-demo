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
OLLAMA_HOST = "ollama.localhost"
OLLAMA_PORT = 11434

# Long enough for a cold model load on a laptop, which is the slow case.
REQUEST_TIMEOUT = 180

FINISH = "finish"


class PlannerError(Exception):
    """The model could not be asked, or did not answer usably."""


def default_url() -> str:
    """The local chat endpoint.

    Addressed by hostname rather than by IP so the call is as identifiable in
    logs as every other backend this agent talks to.
    """
    return f"http://{OLLAMA_HOST}:{OLLAMA_PORT}/api/chat"


def model_from_environment() -> str:
    """The configured model, defaulting to the one this demo is built around."""
    return os.environ.get("OLLAMA_MODEL") or DEFAULT_MODEL


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

    def __init__(self, session, url, model=DEFAULT_MODEL, max_retries=2):
        self.session = session
        self.url = url
        self.model = model
        self.max_retries = max_retries

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
            content = self._chat(attempt, schema)
            try:
                action = json.loads(content)
            except ValueError:
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
            if name not in allowed:
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
            "options": {"temperature": 0},
            "messages": messages,
        }
        try:
            response = self.session.post(
                self.url, json=payload, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
        except requests.exceptions.RequestException as exc:
            raise PlannerError(f"Ollama request failed: {exc}") from exc

        try:
            return response.json()["message"]["content"]
        except (KeyError, ValueError, TypeError) as exc:
            raise PlannerError(f"Ollama reply was not usable: {exc}") from exc

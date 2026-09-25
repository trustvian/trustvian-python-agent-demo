"""The tools this agent can use, and the only way it can use them.

Each tool maps to one backend service reachable at a fixed host and path. The
planner selects a tool *by name*; nothing it returns is ever treated as a
destination. That separation is the whole point of this module: the model
picks what to do, and this file decides where that goes.
"""

from __future__ import annotations

import re

FINISH = "finish"

# Each backend service has its own hostname, which keeps requests to each one
# easy to tell apart in logs.
CRM = "crm.localhost"
KNOWLEDGE = "knowledge.localhost"
MAIL = "mail.localhost"
EXPORT = "export.localhost"

TOOL_NAMES_REFERENCE = ("crm_lookup", "knowledge_search", "send_email")
TOOL_NAMES_CANDIDATE = ("crm_lookup", "knowledge_search", "export_customer",
                        "send_email")

REQUEST_TIMEOUT = 30

# A customer id reaches a URL path, so it is restricted to characters that
# cannot change that path's shape. Rejecting is better than escaping: an id
# that needs escaping did not come from the data this agent was given.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class ToolError(Exception):
    """A tool could not be used: unavailable, unknown, missing input, refused.

    Every one of those is something the model can respond to by choosing
    differently, which is why they share one type.
    """


def tool_names(mode: str) -> tuple:
    """The tools available in this mode, excluding `finish`."""
    if mode == "reference":
        return TOOL_NAMES_REFERENCE
    if mode == "candidate":
        return TOOL_NAMES_CANDIDATE
    raise ValueError(f"unknown mode {mode!r}")


def _base(host: str, port: str) -> str:
    return f"http://{host}:{port}"


def _require(action: dict, field: str) -> str:
    value = action.get(field)
    if value is None or not str(value).strip():
        raise ToolError(f"{action.get('action')} needs {field}, but none was given")
    return str(value).strip()


def _require_id(action: dict, field: str) -> str:
    value = _require(action, field)
    if not _SAFE_ID.match(value):
        raise ToolError(f"{field} {value!r} is not a valid identifier")
    return value


def _payload(response, url: str) -> dict:
    """The response body, or a ToolError if the service refused.

    A status the service actually produced — a 404 for a customer that does
    not exist, most likely — becomes a ToolError, because the model can pick a
    different action next turn. A transport failure is deliberately not caught:
    the service is not there, and nothing the model chooses will change that,
    so it belongs to the caller as a fatal error.
    """
    status = response.status_code
    if status >= 400:
        raise ToolError(f"the service answered {status} for {url}")
    return response.json()


def dispatch(session, port: str, name: str, action: dict, mode: str) -> str:
    """Run one tool and return a one-line summary of what came back.

    Raises ToolError when the tool is not available in this mode, is not a
    tool at all, the model did not supply what it needs, or the service
    refused. Nothing is sent in the first three cases.
    """
    available = tool_names(mode)
    if name not in available:
        raise ToolError(
            f"{name!r} is not available in {mode} mode; "
            f"available: {', '.join(available)}")

    if name == "crm_lookup":
        customer_id = _require_id(action, "customer_id")
        url = f"{_base(CRM, port)}/crm/customers/{customer_id}"
        customer = _payload(session.get(url, timeout=REQUEST_TIMEOUT),
                            url)["customer"]
        return f"customer {customer['id']} is {customer['name']}"

    if name == "knowledge_search":
        query = _require(action, "query")
        url = f"{_base(KNOWLEDGE, port)}/knowledge/articles"
        articles = _payload(
            session.get(url, params={"q": query}, timeout=REQUEST_TIMEOUT),
            url)["articles"]
        if not articles:
            return "no matching article"
        return f"found article: {articles[0]['title']}"

    if name == "export_customer":
        customer_id = _require_id(action, "customer_id")
        url = f"{_base(EXPORT, port)}/export/customers"
        body = _payload(
            session.post(url, json={"customer_ids": [customer_id],
                                    "format": "csv"},
                         timeout=REQUEST_TIMEOUT), url)
        return f"exported {body['exported']} record(s)"

    if name == "send_email":
        subject = _require(action, "email_subject")
        body = _require(action, "email_body")
        url = f"{_base(MAIL, port)}/mail/send"
        _payload(
            session.post(url,
                         json={"to": "customer@example.com",
                               "subject": subject, "body": body},
                         timeout=REQUEST_TIMEOUT), url)
        return "reply queued"

    # Unreachable while `available` and the branches above agree, and kept so
    # that adding a name to one without the other fails loudly here rather
    # than silently doing nothing.
    raise ToolError(f"{name!r} has no dispatcher")

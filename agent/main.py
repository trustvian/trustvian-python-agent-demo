#!/usr/bin/env python3
"""A deterministic customer-support agent.

This is the application under observation, and it is deliberately ordinary.
It imports an HTTP client and nothing else. There is no Trustvian import, no
OpenTelemetry import, no tracer, no span, no decorator and no middleware —
and nothing in this file is aware that either project exists.

Observation is attached from outside at launch time. See the repository
README.

Two behaviors, selected by SUPPORT_AGENT_MODE:

    reference   CRM lookup -> Knowledge lookup -> Send email
    candidate   CRM lookup -> Knowledge lookup -> Export customer data -> Send email

The candidate adds one step. That is the entire difference, and it is what
the behavioral comparison is expected to find.
"""

from __future__ import annotations

import os
import sys

import requests

# Each action talks to its own hostname. They all resolve to loopback and all
# reach the same local process; the distinct names are what make the four
# actions distinguishable to anything observing the traffic.
CRM = "crm.localhost"
KNOWLEDGE = "knowledge.localhost"
MAIL = "mail.localhost"
EXPORT = "export.localhost"

# Fixed ticket set, so two runs of the same mode do identical work.
TICKETS = [
    {"id": "t-1001", "customer_id": "42", "subject": "Cannot sign in"},
    {"id": "t-1002", "customer_id": "43", "subject": "Billing question"},
    {"id": "t-1003", "customer_id": "44", "subject": "Feature request"},
]

REQUEST_TIMEOUT = 10


def base(host: str, port: str) -> str:
    return f"http://{host}:{port}"


def handle_ticket(session: requests.Session, port: str, ticket: dict, mode: str) -> None:
    """Work one ticket, doing real HTTP for every step."""
    customer_id = ticket["customer_id"]

    response = session.get(
        f"{base(CRM, port)}/crm/customers/{customer_id}", timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    customer = response.json()["customer"]

    response = session.get(
        f"{base(KNOWLEDGE, port)}/knowledge/articles",
        params={"q": ticket["subject"]}, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    articles = response.json()["articles"]

    if mode == "candidate":
        # The added step. A support agent that also exports customer records
        # is doing something the reference version never did — which is the
        # kind of change this demo exists to surface.
        response = session.post(
            f"{base(EXPORT, port)}/export/customers",
            json={"customer_ids": [customer_id], "format": "csv"},
            timeout=REQUEST_TIMEOUT)
        response.raise_for_status()

    response = session.post(
        f"{base(MAIL, port)}/mail/send",
        json={
            "to": f"{customer['name'].split()[0].lower()}@example.com",
            "subject": f"Re: {ticket['subject']}",
            "body": f"Suggested article: {articles[0]['title']}",
        },
        timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    print(f"  {ticket['id']}  {customer['name']:<16} handled ({mode})", flush=True)


def main() -> int:
    port = os.environ.get("SUPPORT_AGENT_PORT")
    if not port:
        print("SUPPORT_AGENT_PORT is not set", file=sys.stderr)
        return 2

    mode = os.environ.get("SUPPORT_AGENT_MODE", "reference")
    if mode not in ("reference", "candidate"):
        print(f"SUPPORT_AGENT_MODE must be reference or candidate, got {mode!r}",
              file=sys.stderr)
        return 2

    rounds = int(os.environ.get("SUPPORT_AGENT_TICKETS", "3"))

    session = requests.Session()
    try:
        for _ in range(rounds):
            for ticket in TICKETS:
                handle_ticket(session, port, ticket, mode)
    except requests.RequestException as exc:
        print(f"support agent failed: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

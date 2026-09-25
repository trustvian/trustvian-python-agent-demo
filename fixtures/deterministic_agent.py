#!/usr/bin/env python3
"""Deterministic support ticket automation.

A fixed, model-free implementation of the same support workflow the main agent
performs. It exists so the end-to-end infrastructure — instrumentation,
collection, ingest, evaluation, behavioral diff and gate — can be verified
without depending on a language model choosing the expected actions.

Works a fixed set of support tickets: looks up the customer in CRM, searches
the knowledge base for an article matching the ticket subject, and emails the
customer a reply suggesting it.

Configuration is via environment variables:

    SUPPORT_AGENT_PORT     required; port the backend services listen on
    SUPPORT_AGENT_MODE     "reference" or "candidate" (default "reference")
    SUPPORT_AGENT_ROUNDS   how many times to work through the ticket list
                            (default 3)

Two modes, selected by SUPPORT_AGENT_MODE:

    reference   CRM lookup -> Knowledge lookup -> Send email
    candidate   CRM lookup -> Knowledge lookup -> Export customer data -> Send email

Candidate mode additionally exports the customer's data before sending the
reply.
"""

from __future__ import annotations

import os
import sys

import requests

# Each backend service has its own hostname, which keeps requests to each
# one easy to tell apart in logs.
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
        # Candidate mode also exports the customer's record before the
        # reply goes out.
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

    rounds_raw = os.environ.get("SUPPORT_AGENT_ROUNDS", "3")
    try:
        rounds = int(rounds_raw)
    except ValueError:
        print(f"SUPPORT_AGENT_ROUNDS must be an integer, got {rounds_raw!r}",
              file=sys.stderr)
        return 2
    if rounds < 1:
        print(f"SUPPORT_AGENT_ROUNDS must be at least 1, got {rounds}",
              file=sys.stderr)
        return 2

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

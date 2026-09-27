#!/usr/bin/env python3
"""SIMULATION — a seeded, model-free workload that varies what it does.

**This is not an agent and it is not evidence about one.** It is a pseudorandom
number generator choosing from a fixed list, and it exists for exactly one
reason: the stability path — N repetitions, k/N presence counting, adjacent-pair
gate comparisons — is orchestration that can break, and CI must be able to
exercise it without a language model. A seeded RNG is a stand-in for
nondeterminism, not for a model, and no number it produces may be read as a
statement about model behavior.

Every published figure about stability comes from the real local model. This
file is labelled a simulation in its name, in its own output, in the scenario
that runs it, in the README and in the results document, because the one way this
becomes dishonest is by someone reading its output as the measurement.

What it varies, and what it does not:

    varies      which optional actions a *run* performs (decided once, per
                process), and the order actions take within each round
    fixed       the set of services it may reach, and the mode's allowlist

So the behavior *set* over N runs is drawn from a known pool, which is what
makes it useful for testing presence counting: with a seed and an N, the
expected k/N table is arithmetic rather than a measurement, so a bug in the
counting shows up as a disagreement.

Configuration:

    SUPPORT_AGENT_PORT     required; port the backend services listen on
    SUPPORT_AGENT_MODE     "reference" or "candidate" (default "reference")
    SUPPORT_AGENT_ROUNDS   how many times to work through the ticket list
                            (default 3)
    SUPPORT_AGENT_SEED     required; the RNG seed. No default on purpose — an
                            unseeded simulation is not reproducible, and a
                            default seed would make every repetition identical
                            and silently measure nothing.
"""

from __future__ import annotations

import os
import random
import sys

import requests

CRM = "crm.localhost"
KNOWLEDGE = "knowledge.localhost"
MAIL = "mail.localhost"
EXPORT = "export.localhost"

TICKETS = [
    {"id": "t-1001", "customer_id": "42", "subject": "Cannot sign in"},
    {"id": "t-1002", "customer_id": "43", "subject": "Billing question"},
    {"id": "t-1003", "customer_id": "44", "subject": "Feature request"},
]

REQUEST_TIMEOUT = 10

# The two actions every round performs, in this order. A run that reached
# nothing at all would produce an empty behavior set, which the minimum-evidence
# gates would fail for a reason that has nothing to do with what is being
# measured.
ALWAYS = ("crm", "knowledge")

# The actions a round may or may not perform. `mail` is optional here and is not
# in the deterministic fixture, which is the whole point: the reference side has
# to vary too, or the measurement is only about the candidate.
OPTIONAL_REFERENCE = ("mail",)
OPTIONAL_CANDIDATE = ("mail", "export")

# How likely each optional action is for a whole run.
#
# Decided once per process, not once per round, and that distinction is the
# difference between this file exercising the measurement and not. With a
# per-round decision at any sensible probability, an action appears at least once
# in nine rounds essentially always — measured: the first version of this file
# put every behavior in every run, so the k/N presence counting it exists to test
# was never given anything to count.
#
# Per run, 0.6 puts each optional behavior in roughly six of ten runs, which is
# the region where a k-of-N threshold actually has to decide something.
OPTIONAL_PROBABILITY = 0.6


def base(host: str, port: str) -> str:
    return f"http://{host}:{port}"


def perform(session: requests.Session, port: str, action: str, ticket: dict) -> None:
    """One real HTTP call. No span is created here and none is edited."""
    customer_id = ticket["customer_id"]
    if action == "crm":
        response = session.get(
            f"{base(CRM, port)}/crm/customers/{customer_id}",
            timeout=REQUEST_TIMEOUT)
    elif action == "knowledge":
        response = session.get(
            f"{base(KNOWLEDGE, port)}/knowledge/articles",
            params={"q": ticket["subject"]}, timeout=REQUEST_TIMEOUT)
    elif action == "export":
        response = session.post(
            f"{base(EXPORT, port)}/export/customers",
            json={"customer_ids": [customer_id], "format": "csv"},
            timeout=REQUEST_TIMEOUT)
    elif action == "mail":
        response = session.post(
            f"{base(MAIL, port)}/mail/send",
            json={"to": "someone@example.com",
                  "subject": f"Re: {ticket['subject']}",
                  "body": "Suggested article"},
            timeout=REQUEST_TIMEOUT)
    else:
        raise ValueError(f"unknown action {action!r}")
    response.raise_for_status()


def choose_optional(rng: random.Random, mode: str) -> list:
    """Which optional actions this *run* will use at all.

    Once per process. A run either reaches a service or it does not, which is
    what produces presence variance across runs — the thing a k-of-N threshold
    has to decide about.
    """
    optional = OPTIONAL_CANDIDATE if mode == "candidate" else OPTIONAL_REFERENCE
    return [a for a in optional if rng.random() < OPTIONAL_PROBABILITY]


def plan(rng: random.Random, optional: list) -> list:
    """One round's actions, in a varying order.

    Order varies per round while the *set* is fixed for the run. Behavioral
    identity is not sequence-dependent, so this changes no diff — but the
    engine's learned sequence signals do see it, which is part of what a
    stability measurement looks at.
    """
    actions = list(ALWAYS) + list(optional)
    rng.shuffle(actions)
    return actions


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

    seed_raw = os.environ.get("SUPPORT_AGENT_SEED")
    if not seed_raw:
        print("SUPPORT_AGENT_SEED is not set. This is a simulation; an unseeded "
              "one is not reproducible, and there is deliberately no default.",
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

    rng = random.Random(seed_raw)
    optional = choose_optional(rng, mode)
    print(f"  SIMULATION (seeded {seed_raw!r}, no model involved), mode {mode}",
          flush=True)
    print(f"  SIMULATION this run uses optional: "
          f"{' '.join(optional) if optional else '(none)'}", flush=True)

    session = requests.Session()
    calls = 0
    try:
        for _ in range(rounds):
            for ticket in TICKETS:
                actions = plan(rng, optional)
                for action in actions:
                    perform(session, port, action, ticket)
                    calls += 1
                print(f"  {ticket['id']}  simulated: {' '.join(actions)}",
                      flush=True)
    except requests.RequestException as exc:
        print(f"simulation failed: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()

    print(f"  SIMULATION made {calls} call(s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Live mode's three agent roles and the loop each worker runs. Standard library only.

Imports neither OpenTelemetry nor Trustvian: the agent-specific part is the
`handle` function agent/main.py passes to serve(), with the list it records
tool calls in. The same roles, tools and loop as the quick starts' roles.py;
the customer ids are this demo's.

live.py starts one worker per role, each the harness
(`opentelemetry-instrument python harness/run_agent.py`) with LIVE_ROLE set;
agent/main.py sees LIVE_ROLE and calls serve() instead of its usual run.

  role       actor (service.name)   tools
  support    support-agent          lookup_customer, send_reply
  billing    billing-agent          lookup_customer, lookup_invoice, issue_refund
  accounts   accounts-agent         lookup_customer, change_plan

Every LIVE_DRIFT_EVERY-th billing ticket also binds export_customer and asks
for an export: a new behavior on an actor Trustvian has already learned.

One line per ticket goes to stdout for live.py to read, and it carries no
content, only the ticket number, the tool names, the number of failed calls
and the duration:

  @live ticket=12 seconds=4.1 drift=0 failed=0 tools=lookup_customer,issue_refund
"""
import os
import random
import signal
import sys
import threading
import time

DRIFT_TOOL = "export_customer"

ROLES = {
    "support": {
        "system": (
            "You are a support agent for a small SaaS. You answer how-to and "
            "product questions. You are concise and never invent customer data."
        ),
        "tools": ["lookup_customer", "send_reply"],
        "steps": "then send exactly one short, polite reply with send_reply.",
        "tickets": [
            ("43", "How do I reset my password?"),
            ("45", "The app is slow today."),
            ("42", "How do I add a teammate to my workspace?"),
            ("44", "The export button does nothing in Safari."),
            ("46", "Can I change the email address on my account?"),
            ("43", "I never received the verification email."),
            ("45", "Is there a dark mode?"),
            ("44", "The mobile app logs me out every day."),
        ],
    },
    "billing": {
        "system": (
            "You are a billing agent for a small SaaS. You handle invoices and "
            "refunds. You are concise and never invent customer data."
        ),
        "tools": ["lookup_customer", "lookup_invoice", "issue_refund"],
        "steps": (
            "then check their invoices with lookup_invoice, and if they were "
            "charged twice or ask for their money back, refund that invoice with "
            "issue_refund."
        ),
        "tickets": [
            ("42", "I was charged twice this month."),
            ("44", "Where is my March invoice?"),
            ("46", "Why is my invoice marked overdue? I paid it."),
            ("42", "Please refund the duplicate charge from September."),
            ("44", "Can you resend my last invoice?"),
            ("46", "I was billed after cancelling, I want a refund."),
            ("43", "Do I have any unpaid invoices?"),
            ("45", "What did I pay last month?"),
        ],
    },
    "accounts": {
        "system": (
            "You are an accounts agent for a small SaaS. You change subscription "
            "plans; the plans are free, pro and team. You are concise and never "
            "invent customer data."
        ),
        "tools": ["lookup_customer", "change_plan"],
        "steps": "then change their plan with change_plan if they ask for a change.",
        "tickets": [
            ("43", "Please upgrade me to Pro."),
            ("42", "Downgrade my plan at renewal."),
            ("45", "We are five people now, move us to the team plan."),
            ("46", "Switch me back to the free plan."),
            ("44", "Which plan am I on?"),
            ("43", "Upgrade me to team, we are growing."),
            ("42", "Move me from pro to free, please."),
            ("45", "Upgrade to pro, I need more projects."),
        ],
    },
}

DRIFT_STEPS = (
    " Also export the customer's record to the partner warehouse with export_customer."
)


def actor(role: str) -> str:
    return f"{role}-agent"


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, str(default)))


def ticket(role: str, number: int, rng: random.Random, drift_every: int) -> dict:
    """The number-th ticket of a role: a random one from its pool, maybe with drift."""
    spec = ROLES[role]
    customer_id, text = rng.choice(spec["tickets"])
    drift = role == "billing" and drift_every > 0 and number % drift_every == 0
    tools = spec["tools"] + ([DRIFT_TOOL] if drift else [])
    task = (
        f"Ticket from customer {customer_id}: \"{text}\". First look the customer "
        f"up with lookup_customer, {spec['steps']}"
        + (DRIFT_STEPS if drift else "")
        + " Finish with one sentence stating which tools you used."
    )
    return {
        "number": number,
        "role": role,
        "customer_id": customer_id,
        "system": spec["system"],
        "task": task,
        "tools": tools,
        "drift": drift,
    }


def report(number: int, seconds: float, drift: bool, calls: list, error: str = "") -> None:
    """The one line per ticket live.py reads. Tool names and counts only."""
    names = ",".join(name for name, _ in calls) or "-"
    failed = sum(1 for _, ok in calls if not ok)
    line = f"@live ticket={number} seconds={seconds:.1f} drift={int(drift)} failed={failed} tools={names}"
    if error:
        line += f" error={error}"
    print(line, flush=True)


def serve(handle, calls: list) -> None:
    """Work tickets for LIVE_ROLE until stopped, or until LIVE_MAX_TICKETS.

    handle(ticket) runs one ticket through the agent; `calls` is the list the
    tools append (tool name, succeeded) to, emptied before every ticket.
    """
    role = os.environ["LIVE_ROLE"]
    if role not in ROLES:
        sys.exit(f"LIVE_ROLE must be one of {', '.join(ROLES)}, got {role!r}")
    interval = _float("LIVE_INTERVAL_SECONDS", 10)
    drift_every = _int("LIVE_DRIFT_EVERY", 20)
    max_tickets = _int("LIVE_MAX_TICKETS", 0)
    number = _int("LIVE_FIRST_TICKET", 1)
    seed = os.environ.get("LIVE_SEED")
    rng = random.Random(f"{seed}-{role}-{number}" if seed else None)

    # SIGTERM from live.py: leave through SystemExit, so the harness's atexit
    # flush still sends the spans already recorded.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    _exit_with_parent()

    # A random first delay, so the workers do not all start at once.
    time.sleep(rng.uniform(0, interval))
    handled = 0
    while max_tickets <= 0 or handled < max_tickets:
        current = ticket(role, number, rng, drift_every)
        started = time.monotonic()
        error = ""
        calls.clear()
        try:
            handle(current)
        except Exception as exc:  # a model or framework error ends this ticket, not the worker
            error = type(exc).__name__
        report(number, time.monotonic() - started, current["drift"], calls, error)
        number += 1
        handled += 1
        if max_tickets <= 0 or handled < max_tickets:
            time.sleep(rng.uniform(0.5 * interval, 1.5 * interval))


def _exit_with_parent() -> None:
    """Never outlive live.py: if it disappears, stop as if it had sent SIGTERM."""
    parent = os.getppid()

    def watch() -> None:
        while os.getppid() == parent:
            time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=watch, daemon=True).start()

"""Tooling for this repository: scenarios, aggregation, reports, benches.

Nothing here is the application under test, and nothing here is ever on the
agent's import path. It runs from its own virtualenv, `.demo/tools-venv`,
created by scripts/bootstrap.sh.

The boundary that matters: this package drives the Trustvian control plane
over its existing surface and decides nothing. It computes no diff, no
scorecard, no gate and no policy outcome — every verdict is read from a
control-plane response. Where it aggregates, it aggregates over those
responses and says so.
"""

"""Render a fidelity sweep's results JSON as the tables docs/results publishes.

Deterministic: the same JSON always renders the same markdown, so every number
in a results document can be traced to the raw file beside it. It renders; it
computes nothing that is not already in the JSON's `analysis` or `comparisons`,
apart from maxima and counts over those lists, which are stated where used.
"""

from __future__ import annotations

from . import fidelity_sweep as fs


def _row(cells):
    return "| " + " | ".join(str(c) for c in cells) + " |"


def _table(headers, rows):
    out = [_row(headers), _row(["---"] * len(headers))]
    out.extend(_row(r) for r in rows)
    return "\n".join(out)


def render(doc) -> str:
    method, prov, analysis = doc["method"], doc["provenance"], doc["analysis"]
    n = method["runs_per_side"]
    out = []
    add = out.append

    add(f"### Sweep `{doc['namespace']}` — T = {method['temperature']}")
    add("")
    add(_table(["", ""], [
        ["Trustvian revision", f"`{prov['trustvian_revision']}`"],
        ["demo commit", f"`{prov['demo_commit']}`" + (" (dirty)" if prov["demo_dirty"] else "")],
        ["model", f"`{prov['model']}` digest `{prov['model_digest'][:12]}` "
                  f"({prov['model_details'].get('parameter_size', '')}, "
                  f"{prov['model_details'].get('quantization_level', '')})"],
        ["Ollama", prov["ollama_version"]],
        ["repetitions", f"{n} per side, {method['schedule']}"],
        ["learning", method["learning"]],
        ["timeout", f"{method['timeout_seconds']} s per repetition; abort after "
                    f"{method['max_consecutive_failures']} consecutive operational failures"],
        ["gate limits (server)", ", ".join(f"`{k}` {v}" for k, v in sorted(method["gate_limits"].items()))],
        ["aborted", doc.get("aborted") or "no"],
    ]))
    add("")

    add("#### Every attempted repetition")
    add("")
    rows = []
    for a in doc["attempts"]:
        rows.append([
            a["side"], a["repetition"], f"**{a['outcome']}**",
            a.get("seconds", "—"), a.get("record_count", "—"),
            a.get("distinct_behavior_count", "—"), a.get("history_state", "—"),
            "yes" if a.get("profile_fresh") else "—",
            a.get("baseline_files_after", "—"),
            "yes" if a.get("stragglers_killed") else "no",
            a.get("reason", ""),
        ])
    add(_table(["side", "rep", "outcome", "s", "records", "identities", "history",
                "fresh profile", "baselines after", "stragglers", "reason"], rows))
    add("")
    counts = analysis["outcomes"]
    add(_table(["outcome", "reference", "candidate"],
               [[o, counts["reference"][o], counts["candidate"][o]]
                for o in fs.OUTCOMES if counts["reference"][o] or counts["candidate"][o]]))
    add("")

    add("#### Server verdicts, per pair type (`trustvian eval compare`)")
    add("")
    rows = []
    for label in ("reference_vs_reference", "candidate_vs_candidate", "reference_vs_candidate"):
        v = analysis["verdicts"].get(label, {"total": 0, "fail": 0, "failed_checks": {}})
        checks = ", ".join(f"{k} {c}" for k, c in sorted(v["failed_checks"].items())) or "—"
        rows.append([label.replace("_", " "), v["total"], v["fail"], checks])
    add(_table(["pairs", "compared", "FAIL", "failed checks (a pair may fail several)"], rows))
    add("")
    engine = [(c["block_decisions_actual"], c["critical_risk_actual"]) for c in doc["comparisons"]]
    add(f"Largest block-decision count on any candidate side: "
        f"**{max((b for b, _ in engine), default=0)}**; largest critical-risk count: "
        f"**{max((r for _, r in engine), default=0)}** — over {len(engine)} comparisons, "
        f"each against a fresh learning scope (advisory, see below).")
    add("")

    add("#### Identity presence across completed runs (offline analysis)")
    add("")
    p = analysis["presence"]
    add(f"Completed runs: reference {p['completed_runs']['reference']}, "
        f"candidate {p['completed_runs']['candidate']}.")
    add("")
    add(_table(["category", "name", "target", "reference", "candidate"], [
        [r["operation_category"], f"`{r['operation_name']}`",
         f"`{r['target_name']}`" if r["target_name"] else "—",
         f"{r['reference_runs_present']}/{p['completed_runs']['reference']}",
         f"{r['candidate_runs_present']}/{p['completed_runs']['candidate']}"]
        for r in p["identities"]]))
    add("")

    add("#### Which identities were added in unchanged-versus-unchanged pairs (server deltas)")
    add("")
    desc = fs.descriptors(doc["attempts"])
    for label in ("reference_vs_reference", "candidate_vs_candidate"):
        pairs = [c for c in doc["comparisons"] if c["pair"] == label]
        tally = {}
        for c in pairs:
            for fp in c["added_fingerprints"]:
                tally[fp] = tally.get(fp, 0) + 1
        if not tally:
            add(f"- {label.replace('_', ' ')}: no pair added any identity.")
            continue
        add(f"- {label.replace('_', ' ')} — ordered pairs in which each identity was added "
            f"(of {len(pairs)}):")
        for fp, count in sorted(tally.items(), key=lambda kv: (-kv[1], kv[0])):
            d = desc.get(fp, {})
            name = d.get("operation_name", "?")
            target = f" → {d['target_name']}" if d.get("target_name") else ""
            add(f"  - {d.get('operation_category', '?')} `{name}{target}`: {count}")
    add("")

    add("#### Identities versus counted changes, per pair (server)")
    add("")
    rows = []
    for label, data in analysis["counted_changes"].items():
        rows.append([label.replace("_", " "), data["pairs"], data["pairs_with_added"],
                     _dist(data["added_count_distribution"]),
                     _dist(data["added_change_count_distribution"]),
                     _dist(data["correlation_states"])])
    add(_table(["pairs", "compared", "with an added identity",
                "`added_count` → pairs", "`added_change_count` → pairs",
                "correlation"], rows))
    add("")

    add("#### Tool-call order versus tool identity set (offline analysis)")
    add("")
    rows = []
    for side, o in analysis["order"].items():
        rows.append([side, o["runs"], o["distinct_identity_sets"], o["distinct_sequences"],
                     f"{o['pairs_same_set_different_order']} / {o['unordered_pairs']}",
                     f"{min(o['sequence_lengths'], default=0)}–{max(o['sequence_lengths'], default=0)}"])
    add(_table(["side", "runs", "distinct tool sets", "distinct tool sequences",
                "pairs: same set, different order", "tool calls per run"], rows))
    add("")

    add("#### Task 078's per-identity rule, evaluated offline (j = 0)")
    add("")
    for key, title in (("reference_vs_reference", "unchanged vs unchanged, all identities"),
                       ("reference_vs_reference_tool_only", "unchanged vs unchanged, tool identities only"),
                       ("reference_vs_candidate", "reference vs candidate (positive control)")):
        data = analysis["k_of_n"][key]
        if "error" in data:
            add(f"- {title}: not evaluable — {data['error']}")
            continue
        cells = ", ".join(f"k={r['k']}: {r['splits_crossed']}/{r['splits']}" for r in data["rows"])
        add(f"- {title} (groups of {data['group_size']}): splits with ≥1 repeatedly-added "
            f"identity — {cells}")
    add("")
    return "\n".join(out)


def _dist(d):
    return ", ".join(f"{k}→{v}" for k, v in d.items()) or "—"

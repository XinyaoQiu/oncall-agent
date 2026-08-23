"""What this thread already established.

A Slack thread is a conversation. The second mention is almost always a follow-up —
"what about server-feed's CPU" only makes sense against the answer to the first — and an
agent that re-derives everything each time wastes the engineer's time and its own budget.

The rows this reads were already being written for evaluation. Nothing new is stored;
what changes is that the agent reads them back.
"""

MAX_PRIOR_STEPS = 8


def summarize(runs: list[dict]) -> str:
    """Render earlier investigations for a prompt. Empty string when this is turn one."""
    if not runs:
        return ""

    sections = []
    for i, run in enumerate(runs, 1):
        lines = [f"Earlier in this thread ({i} of {len(runs)}): {run['alert_name']}"]

        if diagnosis := run.get("diagnosis"):
            if cause := diagnosis.get("likely_cause"):
                confidence = run.get("confidence", "unknown")
                lines.append(f"  concluded ({confidence} confidence): {cause}")

        # What was looked at matters as much as what was concluded: repeating a search
        # that already came back empty is the most common way a follow-up wastes a round.
        checked = []
        for step in (run.get("steps") or [])[:MAX_PRIOR_STEPS]:
            target = next(
                (v for k, v in (step.get("args") or {}).items()
                 if k in ("pattern", "path", "expr", "repo")),
                "",
            )
            first_line = (step.get("observation") or "").splitlines()[:1]
            summary = first_line[0][:90] if first_line else ""
            checked.append(f"  {step['tool']} {target}: {summary}")
        if checked:
            lines.append("  already checked:")
            lines.extend(checked)

        sections.append("\n".join(lines))

    return (
        "\n\n".join(sections)
        + "\n\nBuild on this. Do not re-run a search that is listed above unless the "
        "engineer's new question needs a different answer from it."
    )

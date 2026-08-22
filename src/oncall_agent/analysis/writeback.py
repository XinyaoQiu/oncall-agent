"""Turn a resolved incident into a rec-knowledge entry, proposed as a PR.

A model-generated write is a side effect, so it goes through review. Contradiction is
handled at write time — the extractor sees existing related entries and says whether it
is adding or superseding, rather than letting two conflicting runbooks coexist and
letting retrieval pick arbitrarily.
"""

import re
from datetime import date

from ..llm import LLMClient
from ..models import Diagnosis, KnowledgeEntry, KnowledgeHit, ThreadMessage
from .thread import format_thread

_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "worth_recording": {
            "type": "boolean",
            "description": "False if the thread has no durable lesson worth keeping",
        },
        "title": {"type": "string"},
        "services": {"type": "array", "items": {"type": "string"}},
        "what_happened": {"type": "string"},
        "root_cause": {"type": "string"},
        "how_to_confirm": {"type": "string"},
        "lesson": {"type": "string"},
        "action": {"type": "string", "enum": ["add", "update"]},
        "supersedes": {
            "type": "string",
            "description": "Path of the entry this replaces, empty when adding",
        },
    },
    "required": ["worth_recording", "title", "what_happened", "root_cause", "action"],
}

SYSTEM_PROMPT = """You extract durable lessons from incident threads for a team knowledge base.

Only record what was actually established in the thread. Do not invent root causes, and
do not restate the alert definition as a finding.

Set worth_recording to false when the thread resolved without a transferable lesson —
a one-off, an unexplained self-heal, or a duplicate of an existing entry. Most alerts do
not deserve an entry, and a base full of low-value near-duplicates retrieves worse than
a small one.
"""


def _render(fields: dict, alert_name: str) -> str:
    services = fields.get("services", [])
    body = f"""---
date: {date.today().isoformat()}
alert: {alert_name}
services: [{', '.join(services)}]
last_verified: {date.today().isoformat()}
drafted_by: oncall-agent
---

# {fields['title']}

## What happened

{fields['what_happened']}

## Root cause

{fields['root_cause']}
"""
    if fields.get("how_to_confirm"):
        body += f"\n## How to confirm\n\n{fields['how_to_confirm']}\n"
    if fields.get("lesson"):
        body += f"\n## Lesson\n\n{fields['lesson']}\n"
    return body


def extract(
    llm: LLMClient,
    alert_name: str,
    messages: list[ThreadMessage],
    diagnosis: Diagnosis | None,
    existing: list[KnowledgeHit],
) -> KnowledgeEntry | None:
    """Draft an entry, or return None when the thread has nothing worth keeping."""
    sections = [
        f"Alert: {alert_name}",
        f"\n# Thread\n{format_thread(messages)}",
    ]

    if diagnosis:
        sections.append(
            f"\n# Agent diagnosis (may have been wrong)\n"
            f"{diagnosis.summary}\ncause: {diagnosis.likely_cause}"
        )

    if existing:
        sections.append(
            "\n# Existing related entries\n"
            "If one of these already covers this, either update it (action=update, set "
            "supersedes) or set worth_recording=false.\n"
            + "\n".join(f"- {h.path}: {h.line}" for h in existing)
        )

    sections.append("\n# Task\nExtract a durable entry, or decline.")

    result = llm.generate_json(
        "\n".join(sections), _EXTRACT_SCHEMA, deep=True, system=SYSTEM_PROMPT
    )

    if not result.get("worth_recording"):
        return None

    slug = re.sub(r"[^a-z0-9]+", "-", result["title"].lower()).strip("-")[:50]
    return KnowledgeEntry(
        title=result["title"],
        filename=f"incidents/{date.today().isoformat()}-{slug}.md",
        body=_render(result, alert_name),
        services=result.get("services", []),
        supersedes=result.get("supersedes") or None,
    )

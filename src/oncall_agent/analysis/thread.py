"""Thread context: what people already established before summoning the agent.

Priors reorder work and avoid repeating what was just said. They never suppress
collection — someone saying "CPU is fine" may have read the wrong dashboard, or read it
before the spike.
"""

from ..llm import LLMClient, LLMUnavailable
from ..models import ThreadMessage

_PRIORS_SCHEMA = {
    "type": "object",
    "properties": {
        "priors": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Findings or hypotheses humans already stated in the thread",
        },
        "mentioned_services": {"type": "array", "items": {"type": "string"}},
        "excluded_directions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["priors"],
}


def format_thread(messages: list[ThreadMessage], limit: int = 30) -> str:
    return "\n".join(f"{m.user}: {m.text}" for m in messages[:limit] if m.text.strip())


def extract_priors(messages: list[ThreadMessage], llm: LLMClient | None) -> list[str]:
    """Summarize what humans already said. Returns [] rather than failing the run."""
    human_messages = [m for m in messages if not m.is_bot and m.text.strip()]
    if len(human_messages) < 2 or llm is None:
        return []

    prompt = (
        "Engineers discussed this alert before asking for help. List what they already "
        "established or ruled out. Be literal — do not infer beyond what was said.\n\n"
        f"{format_thread(human_messages)}"
    )

    try:
        result = llm.generate_json(prompt, _PRIORS_SCHEMA)
    except LLMUnavailable:
        return []

    priors = list(result.get("priors", []))
    for service in result.get("mentioned_services", []):
        priors.append(f"someone mentioned {service}")
    for excluded in result.get("excluded_directions", []):
        # Recorded as a human claim, not as established fact.
        priors.append(f"a human believes this is not: {excluded}")
    return priors

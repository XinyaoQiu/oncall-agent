"""Markdown → Slack mrkdwn.

Slack's mrkdwn looks like Markdown and is not Markdown: bold is one asterisk, links are
`<url|text>`, and `#` headings and `-` bullets are literal characters. A reply written in
Markdown does not fail here, it renders wrong — asterisks and brackets shown to the reader
as punctuation — so the conversion happens once, at the adapter boundary, on everything
that leaves for Slack.

Code fences are passed through untouched: inside them the punctuation is the content.
"""

import re

_FENCE = re.compile(r"(```)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.DOTALL)
_LINK = re.compile(r"!?\[([^\]\n]*)\]\(\s*<?([^)\s]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_BULLET = re.compile(r"^(\s*)[-*+]\s+", re.MULTILINE)


def _convert(text: str) -> str:
    text = _LINK.sub(
        lambda m: f"<{m.group(2)}|{m.group(1)}>" if m.group(1) else f"<{m.group(2)}>", text
    )
    text = _HEADING.sub(r"*\1*", text)
    text = _BOLD.sub(r"*\1*", text)
    text = _BULLET.sub(r"\1• ", text)
    return text


def to_mrkdwn(text: str) -> str:
    """Rewrite Markdown emphasis, links and bullets as Slack mrkdwn."""
    if not text:
        return ""
    parts = _FENCE.split(text)
    out: list[str] = []
    inside = False
    for part in parts:
        if part == "```":
            inside = not inside
            out.append(part)
            continue
        out.append(part if inside else _convert(part))
    return "".join(out)

"""
Plan parsing utilities.
Handles extraction and cleaning of implementation plans from LLM responses.
"""

import re
from typing import Optional


# Regex to extract plan content from <plan>...</plan> tags
_PLAN_RE = re.compile(r"<plan>\s*(.*?)\s*</plan>", re.DOTALL)


def _strip_plan_preamble(text: str) -> str:
    """Remove conversational preamble before the first plan heading."""
    lines = text.split("\n")
    first_heading_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            first_heading_idx = i
            break
    if first_heading_idx is None or first_heading_idx == 0:
        return text
    # Check if pre-heading text looks conversational (not plan content)
    preamble = "\n".join(lines[:first_heading_idx]).strip()
    if not preamble:
        return text
    conversational = any(m in preamble.lower() for m in [
        "let me", "i'll", "i will", "based on", "looking at",
        "now i have", "i need", "i can see", "good —", "good -",
        "here's", "here is", "i've", "i have enough",
        "the user wants", "i should", "i need to", "let me verify",
        "let me check", "i want to", "now let me", "first, let me",
    ])
    if conversational:
        return "\n".join(lines[first_heading_idx:])
    return text


def _extract_plan(text: str) -> Optional[str]:
    """Extract content between <plan>...</plan> tags."""
    m = _PLAN_RE.search(text)
    return m.group(1).strip() if m else None
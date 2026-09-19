"""Disabled legacy model boundary; production runs in GitHub Actions."""
from __future__ import annotations

import json
import re


def call(*args, **kwargs):
    """Reject legacy API calls instead of contacting a model provider."""
    raise RuntimeError(
        "Aether model API calls are disabled. Use the GitHub Actions article pipeline."
    )


def parse_json(text):
    """Grok wraps JSON in prose/fences and citation markers."""
    text = re.sub(r"<<ccr:[^>]+>>", "", text)
    match = re.search(r"\[.*\]|\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group())
    except json.JSONDecodeError:
        return None

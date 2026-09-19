"""Compatibility boundary for the Codex/Computer-Use model workflow.

The Aether repository deliberately does not contain a model endpoint client or
model credential.  Daily research is performed in the authenticated Codex
workflow described in ``automation/daily-lead-pipeline.md``; its structured
results are then validated and stored by this repository.  The stage modules
still import ``llm`` for compatibility with older runs, so ``call`` fails with
an explicit migration message instead of attempting a network request.
"""
from __future__ import annotations

import json
import re


def call(*args, **kwargs):
    """Reject legacy API calls instead of contacting a model provider."""
    raise RuntimeError(
        "Aether model API calls are disabled. Run the Codex/Computer-Use daily "
        "workflow and import its validated artifacts instead."
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

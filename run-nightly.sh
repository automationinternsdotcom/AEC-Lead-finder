#!/bin/bash
# Aether no-key source preflight.
# The daily Codex automation owns browser research, enrichment, and the internal
# report. This wrapper is intentionally limited to deterministic validation so a
# legacy LaunchAgent cannot silently fall back to an API/model runner.

set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

RUNNER="${RUNNER:-$(cd "$(dirname "$0")" && pwd)}"
cd "$RUNNER"
python3 scripts/validate_source_list.py
echo "Source preflight complete; use the Codex daily automation for browser research and Jon's internal report."

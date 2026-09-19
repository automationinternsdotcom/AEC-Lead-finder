#!/bin/bash
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
cd "$(dirname "$0")"
DATA_ROOT="${AETHER_DATA_ROOT:-/Users/openclaw/Code/AEC-Lead-finder}"
export DB_PATH="$DATA_ROOT/scout.db"
export RESULTS_DIR="$DATA_ROOT/results"
export AETHER_SALES_DB_PATH="$DATA_ROOT/aether_sales.sqlite"
python3 "$PWD/scripts/validate_source_list.py"
if [[ "$*" != *"--handoff"* ]]; then
  echo "daily ingestion requires a validated Codex-produced --handoff; no model API runner is configured" >&2
  exit 2
fi
exec uv run --env-file "$DATA_ROOT/.env" python -m integration.daily --enroll-draft "$@"

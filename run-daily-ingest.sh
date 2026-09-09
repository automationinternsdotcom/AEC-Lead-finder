#!/bin/bash
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
cd "$(dirname "$0")"
DATA_ROOT="${AETHER_DATA_ROOT:-/Users/openclaw/Code/AEC-Lead-finder}"
export DB_PATH="$DATA_ROOT/scout.db"
export RESULTS_DIR="$DATA_ROOT/results"
export AETHER_SALES_DB_PATH="$DATA_ROOT/aether_sales.sqlite"
export GROK_MODEL=grok-4.3
export EXTRACTOR_MODEL=grok-4.3
exec uv run --env-file "$DATA_ROOT/.env" python -m integration.daily --enroll-draft "$@"

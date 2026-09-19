#!/bin/sh
set -e
cd "$(dirname "$0")/scout"
python3 ../scripts/validate_source_list.py
if command -v uv >/dev/null 2>&1; then
  PYTHON="uv run python3"
else
  PYTHON="${PYTHON:-python3}"
fi
$PYTHON csvio.py
$PYTHON db.py
$PYTHON extractor.py
$PYTHON article_judge.py
$PYTHON agent_lead_enrichment.py --self-test
$PYTHON apollo_api.py --self-test
$PYTHON apollo_lead_enrichment.py --self-test
$PYTHON score_leads.py --self-check
$PYTHON build_email.py --self-check
echo "all checks passed"

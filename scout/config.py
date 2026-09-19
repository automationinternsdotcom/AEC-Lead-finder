"""Configuration for the GPS-style AEC scout pipeline."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
ENV = dotenv_values(REPO / ".env")


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key) or ENV.get(key) or default


def _path(value: str) -> str:
    path = Path(value)
    return str(path if path.is_absolute() else REPO / path)


# Model research is performed by the Codex/Computer-Use daily workflow.  Keep
# these compatibility names so older stage constructors can still be imported,
# but do not load an endpoint or credential in the repository runtime.
MODEL_PROVIDER = _get("AETHER_MODEL_PROVIDER", "codex-ui")
GROK_MODEL = _get("AETHER_MODEL_NAME", "codex-ui")
TREG_FALLBACK_ENABLED = _get("TREG_FALLBACK_ENABLED", "false").lower() in {"true", "1", "yes"}
TREG_BUDGET_USD = float(_get("TREG_BUDGET_USD", "5"))
EXTRACTOR_MODEL = GROK_MODEL
DB_PATH = _path(_get("DB_PATH", "scout.db"))
RESULTS_DIR = _path(_get("RESULTS_DIR", "results"))
NEWS_WEBSITES_CSV = _path(_get("NEWS_WEBSITES_CSV", "news_websites.csv"))
MAPSDATA_CSV = _get("MAPSDATA_CSV", "")
MAPSDATA_JOB_ID = _get("MAPSDATA_JOB_ID", "")
MAPSDATA_JOB_IDS = _get("MAPSDATA_JOB_IDS", "")
SALES_NAVIGATOR_CSV = _get("SALES_NAVIGATOR_CSV", _get("SALES_NAV_CSV", ""))
COSTAR_TENANT_CSV = _get("COSTAR_TENANT_CSV", "")

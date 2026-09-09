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


BASE_URL = _get("CLIPROXY_BASE_URL", "http://localhost:8317/v1")
API_KEY = _get("CLIPROXY_API_KEY", "ado-local-dev")
GROK_MODEL = _get("GROK_MODEL", "grok-4.3")
TREG_FALLBACK_ENABLED = _get("TREG_FALLBACK_ENABLED", "false").lower() in {"true", "1", "yes"}
TREG_BUDGET_USD = float(_get("TREG_BUDGET_USD", "5"))
EXTRACTOR_MODEL = _get("EXTRACTOR_MODEL", "grok-4.3")
DB_PATH = _path(_get("DB_PATH", "scout.db"))
RESULTS_DIR = _path(_get("RESULTS_DIR", "results"))
NEWS_WEBSITES_CSV = _path(_get("NEWS_WEBSITES_CSV", "news_websites.csv"))

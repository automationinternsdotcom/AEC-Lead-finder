# /// script
# requires-python = ">=3.12"
# dependencies = ["certifi", "dnspython", "feedparser", "httpx", "pydantic>=2.8", "python-dotenv"]
# ///
"""Canonical Aether AEC Scout V2 entrypoint.

The CLI remains the compatibility boundary used by local and GitHub automation.
All stages run in-process and persist resumable state before the next begins.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from v2.orchestrator import PipelineOptions, PipelineRunner

import config


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--workers", type=int, default=5)
    value.add_argument("--treg-go", action="store_true", default=config.TREG_FALLBACK_ENABLED, help="use Treg when Grok finds no usable email")
    value.add_argument("--treg-budget-usd", type=float, default=config.TREG_BUDGET_USD, help="cumulative Treg cache spend ceiling, including resumes")
    value.add_argument(
        "--discover-states",
        type=int,
        default=0,
        help="accepted for GPS CLI compatibility; curated AEC discovery remains Arizona-only",
    )
    value.add_argument(
        "--apollo-go", action="store_true", help="authorize billable Apollo lookups"
    )
    value.add_argument(
        "--apollo-phones",
        action="store_true",
        help="separately authorize Apollo phone reveal (requires --apollo-go and APOLLO_WEBHOOK_URL)",
    )
    value.add_argument(
        "--max-articles",
        type=int,
        default=0,
        help="qualify at most N new candidates; deferred candidates enter review (0 = no limit)",
    )
    value.add_argument(
        "--since", default=(date.today() - timedelta(days=1)).isoformat()
    )
    value.add_argument("--stamp", default=date.today().isoformat())
    value.add_argument("--run-id", default="", help="persistent UUID for resume/retry")
    value.add_argument(
        "--resume", action="store_true", help="resume an existing --run-id"
    )
    value.add_argument(
        "--retry-review",
        action="store_true",
        help="retry eligible quarantined records while resuming",
    )
    value.add_argument(
        "--newsapi", action="store_true", help="manually enable the NewsAPI adapter"
    )
    value.add_argument(
        "--apify",
        action="store_true",
        help="manually enable the Apify/Facebook adapter",
    )
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.apollo_phones and not args.apollo_go:
        print("ERROR: --apollo-phones requires --apollo-go", file=sys.stderr)
        return 2
    options = PipelineOptions(
        db_path=config.DB_PATH,
        results_dir=config.RESULTS_DIR,
        sources_csv=config.NEWS_WEBSITES_CSV,
        stamp=args.stamp,
        since=args.since,
        workers=args.workers,
        max_articles=args.max_articles,
        run_id=args.run_id,
        resume=args.resume,
        retry_review=args.retry_review,
        apollo_go=args.apollo_go,
        treg_go=args.treg_go,
        treg_budget_usd=args.treg_budget_usd,
        apollo_phones=args.apollo_phones,
        phone_webhook=os.environ.get("APOLLO_WEBHOOK_URL", ""),
        newsapi=args.newsapi,
        apify=args.apify,
        grok_model=config.GROK_MODEL,
        extractor_model=config.EXTRACTOR_MODEL,
    )
    try:
        result = PipelineRunner(options).run()
    except Exception as exc:  # noqa: BLE001 - CLI boundary reports stage failures
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"run_id={result.run_id}", file=sys.stderr)
    print(f"manifest={result.manifest_path}", file=sys.stderr)
    if _flag("AETHER_INTEGRATION_ENABLED"):
        print("== sales handoff: enqueue V2 contacts ==", file=sys.stderr)
        try:
            enqueue_sales_handoff(result)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        except subprocess.CalledProcessError as exc:
            print(
                f"ERROR: sales handoff failed with exit {exc.returncode}",
                file=sys.stderr,
            )
            return 1
    return 0


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().casefold() in {"1", "true", "yes", "on"}


def enqueue_sales_handoff(result, *, run=subprocess.run) -> None:
    """Pass V2's hashed, typed sales handoff to the local integration."""
    handoff_path = result.paths.get("sales_handoff", "")
    if not handoff_path:
        raise ValueError("V2 export did not return a sales handoff path")
    run(
        [
            "uv",
            "run",
            "--project",
            str(REPO_ROOT),
            "python",
            "-m",
            "integration.cli",
            "enqueue-handoff",
            handoff_path,
        ],
        cwd=REPO_ROOT,
        check=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())

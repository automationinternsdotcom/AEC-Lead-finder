#!/usr/bin/env python3
"""Explicit-only Aether archive backfill and company enrichment entrypoint."""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]
SCOUT_DIR = REPO_ROOT / "scout"
for path in (str(SCRIPT_DIR), str(SCOUT_DIR), str(REPO_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from bulk_lib import BulkOptions, BulkRunner  # noqa: E402


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--since", required=True, help="inclusive YYYY-MM-DD")
    value.add_argument("--until", required=True, help="inclusive YYYY-MM-DD")
    value.add_argument(
        "--archive-until",
        default="",
        help="optional archive cutoff when later dates are supplied by a seed run",
    )
    value.add_argument("--output", type=Path, required=True)
    value.add_argument("--sources", type=Path, default=REPO_ROOT / "news_websites.csv")
    value.add_argument("--workers", type=int, default=5)
    value.add_argument("--treg-go", action="store_true", help="recover remaining emails through Treg after Grok")
    value.add_argument("--treg-budget-usd", type=float, default=5.0)
    value.add_argument("--recover-sales-with-treg", type=Path, help="existing sales DB: recover all companies without a usable recipient, using saved research")
    value.add_argument("--treg-limit", type=int, default=0)
    value.add_argument(
        "--treg-company-id",
        action="append",
        default=[],
        help="exact company ID to recover; repeat for a reviewed priority batch",
    )
    value.add_argument("--treg-cache-only", action="store_true", help="retry using cached responses only; no Treg network or paid calls")
    value.add_argument("--import-reviewed-treg-contacts", type=Path, help="import reviewed named contacts from cached official citations without paid calls")
    value.add_argument("--export-treg-recovery", action="store_true", help="refresh recovery exports against live suppressions without any provider calls")
    value.add_argument("--refresh-sales-inventory", action="store_true", help="merge newly missing or invalid-only companies into the saved recovery cohort")
    value.add_argument("--apply-recovered-leads", action="store_true", help="enqueue recovered recipients through the existing sales handoff; never enroll")
    value.add_argument("--model", default="grok-4.3")
    value.add_argument("--run-id", default="")
    value.add_argument("--resume", action="store_true")
    value.add_argument(
        "--refresh-why-lines",
        action="store_true",
        help=(
            "create a versioned single template-rendered why-line revision using "
            "at most one Grok call per existing deduplicated company"
        ),
    )
    value.add_argument(
        "--enrich-recipients",
        action="store_true",
        help=(
            "research up to three current decision makers per sendable company, "
            "then public contact details and optional Apollo fallback"
        ),
    )
    value.add_argument(
        "--build-sales-handoff",
        action="store_true",
        help=(
            "build and validate the hashed local sales_handoff.json from a "
            "completed recipient revision; makes no provider calls"
        ),
    )
    value.add_argument(
        "--existing-sales-db",
        type=Path,
        help=(
            "optional existing integration SQLite database used only to block "
            "likely cross-run duplicate events in --build-sales-handoff"
        ),
    )
    value.add_argument(
        "--apollo-go",
        action="store_true",
        help="authorize email-only Apollo fallback for people with no public email or phone",
    )
    value.add_argument(
        "--apollo-cap",
        type=int,
        default=444,
        help="hard ceiling on new Apollo person-match requests (default: 444)",
    )
    value.add_argument(
        "--why-limit",
        type=int,
        help="optional pilot size for --refresh-why-lines; resume without it to finish",
    )
    value.add_argument(
        "--reuse-discovery-corpus",
        action="store_true",
        help="resume from already persisted discovery pages without crawling sites again",
    )
    value.add_argument("--batch-size", type=int, default=20)
    value.add_argument("--seed-db", type=Path)
    value.add_argument("--seed-run-id", default="")
    value.add_argument(
        "--corpus-db",
        type=Path,
        help="reuse verified saved article artifacts from an earlier bulk discovery database",
    )
    value.add_argument(
        "--corpus-run-id",
        default="",
        help="source run ID paired with --corpus-db",
    )
    value.add_argument(
        "--no-search-fallback",
        action="store_true",
        help="disable Grok search fallback for uncovered/incomplete sources",
    )
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.recover_sales_with_treg:
        if args.export_treg_recovery:
            from integration.treg_recovery import build_handoff
            from integration.handoff import load_handoff
            plan=json.loads((args.output/'inventory.json').read_text())
            if plan['sales_db'] != str(args.recover_sales_with_treg.resolve()):
                raise ValueError('recovery export sales DB differs from inventory')
            results=[json.loads(path.read_text()) for path in (args.output/'companies').glob('*.json')]
            path=build_handoff(plan,results,args.output)
            handoff=load_handoff(path)
            print(json.dumps({'company_contacts':len(handoff.recipients),'distinct_emails':len({r.email for r in handoff.recipients}),'paid_provider_calls':0,'csv':str(args.output/'recovered-leads.csv')}))
            return 0
        if not args.treg_go and not args.treg_cache_only:
            raise ValueError("--recover-sales-with-treg requires --treg-go")
        if args.import_reviewed_treg_contacts:
            from integration.treg_recovery import import_reviewed_contacts
            result=import_reviewed_contacts(args.recover_sales_with_treg,args.output,args.import_reviewed_treg_contacts,apply=args.apply_recovered_leads)
            print(json.dumps(result,indent=2))
            return 0
        from integration.treg_recovery import recover
        result = recover(args.recover_sales_with_treg.resolve().parent, args.recover_sales_with_treg,
            args.output, workers=args.workers, budget_usd=args.treg_budget_usd,
            limit=args.treg_limit, apply=args.apply_recovered_leads, refresh_inventory=args.refresh_sales_inventory,
            cache_only=args.treg_cache_only, company_ids=args.treg_company_id)
        print(json.dumps(result, indent=2))
        return 0 if not result["outcomes"].get("deferred") else 1
    if args.resume and not args.run_id:
        print("ERROR: --resume requires --run-id", file=sys.stderr)
        return 2
    if args.refresh_why_lines and not args.resume:
        print("ERROR: --refresh-why-lines requires --resume", file=sys.stderr)
        return 2
    if args.enrich_recipients and not args.resume:
        print("ERROR: --enrich-recipients requires --resume", file=sys.stderr)
        return 2
    if args.build_sales_handoff and not args.resume:
        print("ERROR: --build-sales-handoff requires --resume", file=sys.stderr)
        return 2
    actions = sum(
        bool(value)
        for value in (
            args.refresh_why_lines,
            args.enrich_recipients,
            args.build_sales_handoff,
        )
    )
    if actions > 1:
        print(
            "ERROR: revision actions are mutually exclusive",
            file=sys.stderr,
        )
        return 2
    if args.apollo_go and not args.enrich_recipients:
        print("ERROR: --apollo-go requires --enrich-recipients", file=sys.stderr)
        return 2
    if args.existing_sales_db and not args.build_sales_handoff:
        print(
            "ERROR: --existing-sales-db requires --build-sales-handoff",
            file=sys.stderr,
        )
        return 2
    if args.apollo_cap < 0:
        print("ERROR: --apollo-cap cannot be negative", file=sys.stderr)
        return 2
    if args.why_limit is not None and not args.refresh_why_lines:
        print("ERROR: --why-limit requires --refresh-why-lines", file=sys.stderr)
        return 2
    if args.why_limit is not None and args.why_limit < 1:
        print("ERROR: --why-limit must be positive", file=sys.stderr)
        return 2
    if bool(args.seed_db) != bool(args.seed_run_id):
        print("ERROR: --seed-db and --seed-run-id must be supplied together", file=sys.stderr)
        return 2
    if bool(args.corpus_db) != bool(args.corpus_run_id):
        print("ERROR: --corpus-db and --corpus-run-id must be supplied together", file=sys.stderr)
        return 2
    if args.corpus_db and args.reuse_discovery_corpus:
        print(
            "ERROR: --corpus-db and --reuse-discovery-corpus are mutually exclusive",
            file=sys.stderr,
        )
        return 2
    options = BulkOptions(
        since=args.since,
        until=args.until,
        archive_until=args.archive_until,
        output_dir=args.output.resolve(),
        sources_csv=args.sources.resolve(),
        workers=max(1, args.workers),
        model=args.model,
        run_id=args.run_id or str(uuid.uuid4()),
        resume=args.resume,
        seed_db=args.seed_db.resolve() if args.seed_db else None,
        seed_run_id=args.seed_run_id,
        corpus_db=args.corpus_db.resolve() if args.corpus_db else None,
        corpus_run_id=args.corpus_run_id,
        search_fallback=not args.no_search_fallback,
        reuse_discovery_corpus=args.reuse_discovery_corpus,
        batch_size=max(1, min(args.batch_size, 25)),
    )
    try:
        runner = BulkRunner(options)
        if args.refresh_why_lines:
            result = runner.refresh_why_lines(limit=args.why_limit)
        elif args.enrich_recipients:
            result = runner.enrich_recipients(
                apollo_go=args.apollo_go,
                apollo_cap=args.apollo_cap,
                treg_go=args.treg_go,
                treg_budget_usd=args.treg_budget_usd,
            )
        elif args.build_sales_handoff:
            result = runner.build_sales_handoff(
                existing_sales_db=(
                    args.existing_sales_db.resolve()
                    if args.existing_sales_db
                    else None
                )
            )
        else:
            result = runner.run()
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

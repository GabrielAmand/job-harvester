import argparse
import json
from pathlib import Path
import sqlite3
import sys
from collections.abc import Sequence

from job_harvester.board_validation import BoardValidator
from job_harvester.batches import DECISIONS, BatchError, BatchStore, start_batch
from job_harvester.career_ops import (
    CareerOpsError,
    CareerOpsEvaluation,
    evaluate_open_batch,
)
from job_harvester.collectors.base import CollectionError
from job_harvester.collectors.france_travail import FranceTravailCollector
from job_harvester.collectors.greenhouse import GreenhouseCollector
from job_harvester.collectors.lever import LeverCollector
from job_harvester.config import (
    ConfigError,
    FranceTravailSource,
    GreenhouseSource,
    LeverSource,
    load_config,
)
from job_harvester.discovery import DiscoveryError, discover_boards
from job_harvester.filters import is_relevant
from job_harvester.models import StoredJob
from job_harvester.registry import BoardRegistry, RegistryError
from job_harvester.revalidation import RevalidationError
from job_harvester.storage import JobStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="job-harvester",
        description="Collect public job listings into a local SQLite database.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect = subparsers.add_parser("collect", help="collect configured job boards")
    collect.add_argument("--config", type=Path, default=Path("config.toml"))
    collect.add_argument("--database", type=Path, default=Path("jobs.sqlite3"))
    inspect = subparsers.add_parser("list", help="list relevant stored jobs")
    inspect.add_argument("--config", type=Path, default=Path("config.toml"))
    inspect.add_argument("--database", type=Path, default=Path("jobs.sqlite3"))
    inspect.add_argument("--new-only", action="store_true")
    export = subparsers.add_parser("export", help="export relevant stored jobs as JSON")
    export.add_argument("--config", type=Path, default=Path("config.toml"))
    export.add_argument("--database", type=Path, default=Path("jobs.sqlite3"))
    export.add_argument("--new-only", action="store_true")
    export.add_argument("--output", type=Path, help="write JSON to a file instead of stdout")
    boards = subparsers.add_parser("boards", help="manage Greenhouse and Lever boards")
    boards.add_argument("--database", type=Path, default=Path("jobs.sqlite3"))
    board_commands = boards.add_subparsers(dest="boards_command", required=True)
    board_commands.add_parser("list", help="list registered boards")
    add = board_commands.add_parser("add", help="add a board manually")
    add.add_argument("provider", choices=("greenhouse", "lever"))
    add.add_argument("slug")
    add.add_argument("--company")
    add.add_argument("--provenance", default="manual")
    for action in ("enable", "disable"):
        command = board_commands.add_parser(action, help=f"{action} a board")
        command.add_argument("provider", choices=("greenhouse", "lever"))
        command.add_argument("slug")
    validate = board_commands.add_parser("validate", help="validate registered boards")
    validate.add_argument("provider", nargs="?", choices=("greenhouse", "lever"))
    validate.add_argument("slug", nargs="?")
    discover = board_commands.add_parser("discover", help="import candidate board URLs")
    discover.add_argument("--input", type=Path, required=True)
    batch = subparsers.add_parser("batch", help="manage persistent review batches")
    batch.add_argument("--database", type=Path, default=Path("jobs.sqlite3"))
    batch_commands = batch.add_subparsers(dest="batch_command", required=True)
    start = batch_commands.add_parser("start", help="create a review batch")
    start.add_argument("--config", type=Path, default=Path("config.toml"))
    start.add_argument("--limit", type=int, default=20)
    evaluate = batch_commands.add_parser(
        "evaluate", help="evaluate the open batch with Career-Ops"
    )
    evaluate.add_argument("--config", type=Path, default=Path("config.toml"))
    evaluate.add_argument("--limit", type=int)
    evaluate.add_argument(
        "--force", action="store_true", help="reevaluate completed jobs"
    )
    batch_commands.add_parser("current", help="show the open batch")
    review = batch_commands.add_parser("review", help="mark a batch job reviewed")
    review.add_argument("source", choices=("greenhouse", "lever", "france_travail"))
    review.add_argument("external_id")
    review.add_argument(
        "--decision", choices=DECISIONS,
        default="undecided",
    )
    batch_commands.add_parser("complete", help="complete the open batch")
    batch_commands.add_parser("abandon", help="abandon the open batch")
    batch_commands.add_parser("list", help="list current and previous batches")
    return parser


def run_collect(config_path: Path, database_path: Path) -> int:
    config = load_config(config_path)
    jobs_by_source: dict[str, list] = {
        "greenhouse": [],
        "lever": [],
        "france_travail": [],
    }
    configured_source_types: set[str] = set()
    greenhouse_sources: dict[str, GreenhouseSource] = {}
    lever_sources: dict[str, LeverSource] = {}
    for source in config.sources:
        configured_source_types.add(source.type)
        if isinstance(source, GreenhouseSource):
            greenhouse_sources.setdefault(source.board_token.casefold(), source)
        elif isinstance(source, FranceTravailSource):
            collected = FranceTravailCollector(source.search_terms).collect()
            jobs_by_source[source.type].extend(collected)
        else:
            lever_sources.setdefault(source.company_slug.casefold(), source)

    if database_path.exists():
        with BoardRegistry(database_path) as registry:
            registered = registry.list(enabled_only=True, valid_only=True)
        for board in registered:
            configured_source_types.add(board.provider)
            company = board.company or board.slug
            if board.provider == "greenhouse":
                greenhouse_sources.setdefault(
                    board.slug.casefold(), GreenhouseSource(company, board.slug)
                )
            else:
                lever_sources.setdefault(
                    board.slug.casefold(), LeverSource(company, board.slug)
                )

    for source in greenhouse_sources.values():
        jobs_by_source["greenhouse"].extend(
            GreenhouseCollector(source.company, source.board_token).collect()
        )
    for source in lever_sources.values():
        jobs_by_source["lever"].extend(
            LeverCollector(source.company, source.company_slug).collect()
        )
    jobs = [job for source_jobs in jobs_by_source.values() for job in source_jobs]
    with JobStore(database_path) as store:
        result = store.upsert(jobs)
        states = {
            (record.job.source, record.job.external_id): record.state
            for record in store.list_jobs()
        }
    for source_type, source_jobs in jobs_by_source.items():
        if source_type not in configured_source_types:
            continue
        unique_jobs = {(job.source, job.external_id) for job in source_jobs}
        new = sum(states[identity] == "new" for identity in unique_jobs)
        updated = sum(states[identity] == "updated" for identity in unique_jobs)
        label = source_type.replace("_", " ").title()
        board_count = ""
        if source_type == "greenhouse":
            board_count = f"{len(greenhouse_sources)} boards; "
        elif source_type == "lever":
            board_count = f"{len(lever_sources)} boards; "
        print(
            f"{label}: {board_count}Found {len(source_jobs)}; "
            f"{new} new; {updated} updated."
        )
    print(f"Total: Found {len(jobs)}; {result.new} new; {result.updated} updated.")
    return 0


def run_boards(args: argparse.Namespace) -> int:
    with BoardRegistry(args.database) as registry:
        if args.boards_command == "list":
            boards = registry.list()
            for board in boards:
                company = board.company or "—"
                enabled = "enabled" if board.enabled else "disabled"
                print(
                    f"{board.provider} | {board.slug} | {company} | "
                    f"{enabled} | {board.validation_status}"
                )
            print(f"{len(boards)} board(s).")
            return 0
        if args.boards_command == "add":
            created = registry.add(
                args.provider,
                args.slug,
                company=args.company,
                provenance=args.provenance,
            )
            action = "Added" if created else "Already registered"
            print(f"{action}: {args.provider}/{args.slug.casefold()}")
            return 0
        if args.boards_command in {"enable", "disable"}:
            enabled = args.boards_command == "enable"
            registry.set_enabled(args.provider, args.slug, enabled)
            print(f"{args.boards_command.title()}d: {args.provider}/{args.slug.casefold()}")
            return 0
        if args.boards_command == "discover":
            discovered = discover_boards(args.input)
            added = sum(
                registry.add(
                    provider,
                    slug,
                    provenance=f"import:{args.input.name}",
                )
                for provider, slug in discovered
            )
            print(f"Discovered {len(discovered)} candidate board(s); {added} added.")
            return 0
        if args.boards_command == "validate":
            if (args.provider is None) != (args.slug is None):
                raise RegistryError("validate requires both provider and slug, or neither")
            if args.provider is None:
                boards = registry.list()
            else:
                board = registry.get(args.provider, args.slug)
                if board is None:
                    raise RegistryError(f"board not found: {args.provider}/{args.slug}")
                boards = [board]
            validator = BoardValidator()
            temporary = False
            for board in boards:
                result = validator.validate(board)
                if result.outcome == "temporary":
                    registry.record_validation(board.provider, board.slug, None)
                    temporary = True
                else:
                    registry.record_validation(
                        board.provider,
                        board.slug,
                        result.outcome,
                        company=result.company,
                    )
                detail = (
                    f"{result.job_count} job(s)"
                    if result.job_count is not None
                    else result.error or ""
                )
                print(f"{board.provider}/{board.slug}: {result.outcome} {detail}".rstrip())
            return 1 if temporary else 0
    return 2


def _print_batch_entries(batch_id: int, entries: list | tuple) -> None:
    reviewed = sum(entry.review_state == "reviewed" for entry in entries)
    print(f"Batch {batch_id}: {reviewed}/{len(entries)} reviewed.")
    for entry in entries:
        job = entry.job
        decision = f" ({entry.decision})" if entry.decision else ""
        print(
            f"{entry.position}. [{entry.review_state}{decision}] "
            f"{job.company} — {job.title} | {job.source}/{job.external_id}"
        )
        mode = "full_remote" if job.full_remote else job.work_mode
        print(f"   {mode} ({job.remote_scope}) | {job.url}")


def run_batch(args: argparse.Namespace) -> int:
    if args.batch_command == "start":
        config = load_config(args.config)
        result = start_batch(args.database, config.filters, limit=args.limit)
        print(
            f"Pending candidates: {result.pending_candidates}; "
            f"revalidated: {result.revalidated}; expired: {result.expired}; "
            f"batch size: {len(result.entries)}."
        )
        _print_batch_entries(result.batch.id, result.entries)
        return 0
    if args.batch_command == "evaluate":
        config = load_config(args.config)

        def progress(index: int, total: int, evaluation: CareerOpsEvaluation) -> None:
            print(
                f"[{index}/{total}] {evaluation.company} — {evaluation.title}"
            )
            if evaluation.status == "completed":
                print(
                    f"      score {evaluation.score:g} — {evaluation.category}"
                )
            else:
                print(f"      failed — {evaluation.error}")

        result = evaluate_open_batch(
            args.database,
            config.career_ops,
            limit=args.limit,
            force=args.force,
            progress=progress,
        )
        categories = {
            name: sum(item.category == name for item in result.evaluations)
            for name in (
                "automatic_skip", "review", "good_candidate", "priority_candidate"
            )
        }
        print(
            f"Evaluated: {len(result.evaluations)}\n"
            f"Skipped: {categories['automatic_skip']}\n"
            f"Review: {categories['review']}\n"
            f"Good: {categories['good_candidate']}\n"
            f"Priority: {categories['priority_candidate']}\n"
            f"Failed: {result.failed}"
        )
        if result.already_completed:
            print(f"Already completed: {result.already_completed}")
        return 1 if result.failed else 0
    with BatchStore(args.database) as store:
        if args.batch_command == "list":
            batches = store.list()
            for batch in batches:
                entries = store.entries(batch.id)
                reviewed = sum(entry.review_state == "reviewed" for entry in entries)
                print(
                    f"{batch.id} | {batch.status} | {reviewed}/{len(entries)} reviewed | "
                    f"requested {batch.requested_limit}"
                )
            print(f"{len(batches)} batch(es).")
            return 0
        if args.batch_command == "current":
            batch = store.current()
            if batch is None:
                print("No open batch.")
                return 0
            _print_batch_entries(batch.id, store.entries(batch.id))
            return 0
        if args.batch_command == "review":
            store.review(args.source, args.external_id, args.decision)
            print(
                f"Reviewed: {args.source}/{args.external_id} ({args.decision})."
            )
            return 0
        if args.batch_command == "complete":
            batch = store.complete()
            print(f"Completed batch {batch.id}.")
            return 0
        if args.batch_command == "abandon":
            batch = store.abandon()
            print(f"Abandoned batch {batch.id}.")
            return 0
    return 2


def relevant_jobs(
    config_path: Path, database_path: Path, *, new_only: bool
) -> list[StoredJob]:
    config = load_config(config_path)
    with JobStore(database_path) as store:
        stored = store.list_jobs(new_only=new_only)
    records = [record for record in stored if is_relevant(record.job, config.filters)]
    if config.filters.remote_policy == "prefer":
        order = {"remote": 0, "hybrid": 1, "unknown": 2, "onsite": 3}
        records.sort(key=lambda record: order[record.job.work_mode])
    return records


def run_list(config_path: Path, database_path: Path, *, new_only: bool) -> int:
    records = relevant_jobs(config_path, database_path, new_only=new_only)
    for index, record in enumerate(records):
        if index:
            print()
        job = record.job
        location = job.location or "Location not specified"
        print(f"[{record.state}] {job.company} — {job.title}")
        print(
            f"{location} | {job.source} | "
            f"{'full_remote' if job.full_remote else job.work_mode} "
            f"({job.remote_scope})"
        )
        print(job.url)
    print(f"\n{len(records)} relevant job(s)." if records else "0 relevant jobs.")
    return 0


def _export_record(record: StoredJob) -> dict[str, str | bool | None]:
    job = record.job
    return {
        "source": job.source,
        "external_id": job.external_id,
        "company": job.company,
        "title": job.title,
        "location": job.location,
        "work_mode": job.work_mode,
        "remote_scope": job.remote_scope,
        "full_remote": job.full_remote,
        "remote_eligibility": job.remote_eligibility,
        "published_at": job.published_at.isoformat() if job.published_at else None,
        "url": job.url,
        "collected_at": job.collected_at.isoformat() if job.collected_at else None,
        "state": record.state,
    }


def run_export(
    config_path: Path,
    database_path: Path,
    *,
    new_only: bool,
    output_path: Path | None,
) -> int:
    records = relevant_jobs(config_path, database_path, new_only=new_only)
    document = json.dumps(
        [_export_record(record) for record in records], indent=2, ensure_ascii=False
    ) + "\n"
    if output_path is None:
        print(document, end="")
    else:
        output_path.write_text(document, encoding="utf-8")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "collect":
            return run_collect(args.config, args.database)
        if args.command == "list":
            return run_list(args.config, args.database, new_only=args.new_only)
        if args.command == "export":
            return run_export(
                args.config,
                args.database,
                new_only=args.new_only,
                output_path=args.output,
            )
        if args.command == "boards":
            return run_boards(args)
        if args.command == "batch":
            return run_batch(args)
    except (
        ConfigError,
        CollectionError,
        DiscoveryError,
        RegistryError,
        BatchError,
        RevalidationError,
        CareerOpsError,
        sqlite3.Error,
        OSError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 2

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from collections.abc import Sequence

from job_harvester.collectors.base import CollectionError
from job_harvester.collectors.france_travail import FranceTravailCollector
from job_harvester.collectors.greenhouse import GreenhouseCollector
from job_harvester.collectors.lever import LeverCollector
from job_harvester.config import (
    ConfigError,
    FranceTravailSource,
    GreenhouseSource,
    load_config,
)
from job_harvester.filters import is_relevant
from job_harvester.models import StoredJob
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
    return parser


def run_collect(config_path: Path, database_path: Path) -> int:
    config = load_config(config_path)
    jobs_by_source: dict[str, list] = {
        "greenhouse": [],
        "lever": [],
        "france_travail": [],
    }
    configured_source_types: set[str] = set()
    for source in config.sources:
        configured_source_types.add(source.type)
        if isinstance(source, GreenhouseSource):
            collected = GreenhouseCollector(source.company, source.board_token).collect()
        elif isinstance(source, FranceTravailSource):
            collected = FranceTravailCollector(source.search_terms).collect()
        else:
            collected = LeverCollector(source.company, source.company_slug).collect()
        jobs_by_source[source.type].extend(collected)
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
        print(
            f"{source_type.replace('_', ' ').title()}: Found {len(source_jobs)}; "
            f"{new} new; {updated} updated."
        )
    print(f"Total: Found {len(jobs)}; {result.new} new; {result.updated} updated.")
    return 0


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
            f"{job.work_mode} ({job.remote_scope})"
        )
        print(job.url)
    print(f"\n{len(records)} relevant job(s)." if records else "0 relevant jobs.")
    return 0


def _export_record(record: StoredJob) -> dict[str, str | None]:
    job = record.job
    return {
        "source": job.source,
        "external_id": job.external_id,
        "company": job.company,
        "title": job.title,
        "location": job.location,
        "work_mode": job.work_mode,
        "remote_scope": job.remote_scope,
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
    except (ConfigError, CollectionError, sqlite3.Error, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 2

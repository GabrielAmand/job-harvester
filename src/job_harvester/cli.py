import argparse
from pathlib import Path
import sqlite3
import sys
from collections.abc import Sequence

from job_harvester.collectors.greenhouse import CollectionError, GreenhouseCollector
from job_harvester.config import ConfigError, load_config
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
    return parser


def run_collect(config_path: Path, database_path: Path) -> int:
    config = load_config(config_path)
    jobs = []
    for source in config.sources:
        jobs.extend(GreenhouseCollector(source.company, source.board_token).collect())
    with JobStore(database_path) as store:
        new_count = store.upsert(jobs)
    print(f"Found {len(jobs)} jobs; {new_count} new.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "collect":
            return run_collect(args.config, args.database)
    except (ConfigError, CollectionError, sqlite3.Error, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 2

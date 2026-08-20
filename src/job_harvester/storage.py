from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from job_harvester.models import Job, StoredJob


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT NOT NULL,
    remote_status TEXT,
    work_mode TEXT NOT NULL DEFAULT 'unknown',
    remote_scope TEXT NOT NULL DEFAULT 'unknown',
    published_at TEXT,
    url TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'seen',
    source_key TEXT,
    full_remote INTEGER NOT NULL DEFAULT 0,
    remote_eligibility TEXT NOT NULL DEFAULT 'unknown',
    description TEXT,
    canonical_url TEXT,
    source_url TEXT,
    application_url TEXT,
    remote_days_per_week INTEGER,
    onsite_days_per_week INTEGER,
    remote_intensity TEXT NOT NULL DEFAULT 'unknown',
    remote_enriched_at TEXT,
    remote_enrichment_version INTEGER,
    source_work_mode TEXT,
    source_full_remote INTEGER,
    UNIQUE (source, external_id)
)
"""


@dataclass(frozen=True, slots=True)
class CollectionResult:
    new: int
    updated: int


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


class JobStore(AbstractContextManager["JobStore"]):
    def __init__(self, path: str | Path) -> None:
        self.connection = sqlite3.connect(path)
        self.connection.execute(SCHEMA)
        columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(jobs)")
        }
        if "state" not in columns:
            self.connection.execute(
                "ALTER TABLE jobs ADD COLUMN state TEXT NOT NULL DEFAULT 'seen'"
            )
        if "work_mode" not in columns:
            self.connection.execute(
                "ALTER TABLE jobs ADD COLUMN work_mode TEXT NOT NULL DEFAULT 'unknown'"
            )
        if "remote_scope" not in columns:
            self.connection.execute(
                "ALTER TABLE jobs ADD COLUMN remote_scope TEXT NOT NULL DEFAULT 'unknown'"
            )
        if "source_key" not in columns:
            self.connection.execute("ALTER TABLE jobs ADD COLUMN source_key TEXT")
        if "full_remote" not in columns:
            self.connection.execute(
                "ALTER TABLE jobs ADD COLUMN full_remote INTEGER NOT NULL DEFAULT 0"
            )
        if "remote_eligibility" not in columns:
            self.connection.execute(
                "ALTER TABLE jobs ADD COLUMN remote_eligibility TEXT NOT NULL DEFAULT 'unknown'"
            )
        if "description" not in columns:
            self.connection.execute("ALTER TABLE jobs ADD COLUMN description TEXT")
        if "canonical_url" not in columns:
            self.connection.execute("ALTER TABLE jobs ADD COLUMN canonical_url TEXT")
        additions = (
            ("source_url", "TEXT"), ("application_url", "TEXT"),
            ("remote_days_per_week", "INTEGER"), ("onsite_days_per_week", "INTEGER"),
            ("remote_intensity", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("remote_enriched_at", "TEXT"), ("remote_enrichment_version", "INTEGER"),
            ("source_work_mode", "TEXT"), ("source_full_remote", "INTEGER"),
        )
        for name, definition in additions:
            if name not in columns:
                self.connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
        self.connection.execute(
            """UPDATE jobs SET source_url=CASE
                 WHEN source='france_travail' THEN
                   'https://candidat.francetravail.fr/offres/recherche/detail/' || external_id
                 ELSE url END
               WHERE source_url IS NULL"""
        )
        self.connection.execute(
            "UPDATE jobs SET source_work_mode=work_mode WHERE source_work_mode IS NULL"
        )
        self.connection.execute(
            "UPDATE jobs SET source_full_remote=full_remote WHERE source_full_remote IS NULL"
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __exit__(self, *args: object) -> None:
        self.close()

    def upsert(self, jobs: Iterable[Job]) -> CollectionResult:
        new_count = 0
        updated_count = 0
        with self.connection:
            self.connection.execute("UPDATE jobs SET state = 'seen' WHERE state != 'seen'")
            for job in jobs:
                existing = self.connection.execute(
                    """
                    SELECT company, title, location, work_mode, remote_scope,
                           published_at, url, full_remote, remote_eligibility, description,
                           source_url, source_work_mode, source_full_remote
                    FROM jobs WHERE source = ? AND external_id = ?
                    """,
                    (job.source, job.external_id),
                ).fetchone()
                collected_at = _timestamp(job.discovered_at())
                current_values = (
                    job.company,
                    job.title,
                    job.location,
                    job.work_mode,
                    job.remote_scope,
                    _timestamp(job.published_at),
                    job.url,
                    int(job.full_remote),
                    job.remote_eligibility,
                    job.description,
                    job.source_url or job.url,
                    job.source_work_mode or job.work_mode,
                    int(job.full_remote if job.source_full_remote is None else job.source_full_remote),
                )
                if existing is None:
                    state = "new"
                    new_count += 1
                elif existing != current_values:
                    state = "updated"
                    updated_count += 1
                else:
                    state = "seen"
                self.connection.execute(
                    """
                    INSERT INTO jobs (
                        source, external_id, company, title, location,
                        work_mode, remote_scope, published_at, url, collected_at, state,
                        source_key, full_remote, remote_eligibility, description,
                        source_url, application_url, remote_days_per_week,
                        onsite_days_per_week, remote_intensity, remote_enriched_at,
                        remote_enrichment_version
                        , source_work_mode, source_full_remote
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source, external_id) DO UPDATE SET
                        company = excluded.company,
                        title = excluded.title,
                        location = excluded.location,
                        work_mode = CASE WHEN jobs.remote_enrichment_version IS NOT NULL
                            THEN jobs.work_mode ELSE excluded.work_mode END,
                        remote_scope = excluded.remote_scope,
                        published_at = excluded.published_at,
                        url = excluded.url,
                        state = excluded.state,
                        source_key = COALESCE(excluded.source_key, jobs.source_key),
                        full_remote = CASE WHEN jobs.remote_enrichment_version IS NOT NULL
                            THEN jobs.full_remote ELSE excluded.full_remote END,
                        remote_eligibility = excluded.remote_eligibility,
                        description = excluded.description
                        , source_url = excluded.source_url
                        , application_url = COALESCE(excluded.application_url, jobs.application_url)
                        , remote_days_per_week = COALESCE(excluded.remote_days_per_week, jobs.remote_days_per_week)
                        , onsite_days_per_week = COALESCE(excluded.onsite_days_per_week, jobs.onsite_days_per_week)
                        , remote_intensity = CASE WHEN excluded.remote_intensity != 'unknown'
                            THEN excluded.remote_intensity ELSE jobs.remote_intensity END
                        , source_work_mode = excluded.source_work_mode
                        , source_full_remote = excluded.source_full_remote
                    """,
                    (
                        job.source,
                        job.external_id,
                        job.company,
                        job.title,
                        job.location,
                        job.work_mode,
                        job.remote_scope,
                        _timestamp(job.published_at),
                        job.url,
                        collected_at,
                        state,
                        job.source_key,
                        int(job.full_remote),
                        job.remote_eligibility,
                        job.description,
                        job.source_url or job.url,
                        job.application_url,
                        job.remote_days_per_week,
                        job.onsite_days_per_week,
                        job.remote_intensity,
                        _timestamp(job.remote_enriched_at),
                        job.remote_enrichment_version,
                        job.source_work_mode or job.work_mode,
                        int(job.full_remote if job.source_full_remote is None else job.source_full_remote),
                    ),
                )
        return CollectionResult(new=new_count, updated=updated_count)

    def list_jobs(self, *, new_only: bool = False) -> list[StoredJob]:
        where = "WHERE state = 'new'" if new_only else ""
        rows = self.connection.execute(
            f"""
            SELECT source, external_id, company, title, location, url,
                   work_mode, remote_scope, published_at, collected_at, state,
                   source_key, full_remote, remote_eligibility, description,
                   source_url, application_url, remote_days_per_week,
                   onsite_days_per_week, remote_intensity, remote_enriched_at,
                   remote_enrichment_version
                   , source_work_mode, source_full_remote
            FROM jobs
            {where}
            ORDER BY company COLLATE NOCASE, title COLLATE NOCASE, external_id
            """
        ).fetchall()
        return [
            StoredJob(
                job=Job(
                    source=row[0],
                    external_id=row[1],
                    company=row[2],
                    title=row[3],
                    location=row[4],
                    url=row[5],
                    work_mode=row[6],
                    remote_scope=row[7],
                    published_at=_datetime(row[8]),
                    collected_at=_datetime(row[9]),
                    source_key=row[11],
                    full_remote=bool(row[12]),
                    remote_eligibility=row[13],
                    description=row[14],
                    source_url=row[15], application_url=row[16],
                    remote_days_per_week=row[17], onsite_days_per_week=row[18],
                    remote_intensity=row[19], remote_enriched_at=_datetime(row[20]),
                    remote_enrichment_version=row[21],
                    source_work_mode=row[22], source_full_remote=bool(row[23]),
                ),
                state=row[10],
            )
            for row in rows
        ]

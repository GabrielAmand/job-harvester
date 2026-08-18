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
                           published_at, url
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
                        work_mode, remote_scope, published_at, url, collected_at, state
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source, external_id) DO UPDATE SET
                        company = excluded.company,
                        title = excluded.title,
                        location = excluded.location,
                        work_mode = excluded.work_mode,
                        remote_scope = excluded.remote_scope,
                        published_at = excluded.published_at,
                        url = excluded.url,
                        state = excluded.state
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
                    ),
                )
        return CollectionResult(new=new_count, updated=updated_count)

    def list_jobs(self, *, new_only: bool = False) -> list[StoredJob]:
        where = "WHERE state = 'new'" if new_only else ""
        rows = self.connection.execute(
            f"""
            SELECT source, external_id, company, title, location, url,
                   work_mode, remote_scope, published_at, collected_at, state
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
                ),
                state=row[10],
            )
            for row in rows
        ]

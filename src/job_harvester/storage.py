from collections.abc import Iterable
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from job_harvester.models import Job


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT NOT NULL,
    remote_status TEXT,
    published_at TEXT,
    url TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    UNIQUE (source, external_id)
)
"""


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


class JobStore(AbstractContextManager["JobStore"]):
    def __init__(self, path: str | Path) -> None:
        self.connection = sqlite3.connect(path)
        self.connection.execute(SCHEMA)
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __exit__(self, *args: object) -> None:
        self.close()

    def upsert(self, jobs: Iterable[Job]) -> int:
        new_count = 0
        with self.connection:
            for job in jobs:
                exists = self.connection.execute(
                    "SELECT 1 FROM jobs WHERE source = ? AND external_id = ?",
                    (job.source, job.external_id),
                ).fetchone()
                collected_at = _timestamp(job.discovered_at())
                self.connection.execute(
                    """
                    INSERT INTO jobs (
                        source, external_id, company, title, location,
                        remote_status, published_at, url, collected_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source, external_id) DO UPDATE SET
                        company = excluded.company,
                        title = excluded.title,
                        location = excluded.location,
                        remote_status = excluded.remote_status,
                        published_at = excluded.published_at,
                        url = excluded.url
                    """,
                    (
                        job.source,
                        job.external_id,
                        job.company,
                        job.title,
                        job.location,
                        job.remote_status,
                        _timestamp(job.published_at),
                        job.url,
                        collected_at,
                    ),
                )
                new_count += exists is None
        return new_count
